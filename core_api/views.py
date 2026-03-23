import json
import os
import random
import logging
import re
import secrets
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from rest_framework import permissions, status, generics
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from kafka import KafkaProducer
from rest_framework import serializers
from django.views.decorators.csrf import csrf_exempt
import requests

from .models import *
from .serializers import *
from .throttles import DynamicQueueThrottle
from .judging import (
    build_hidden_testcases,
    get_hidden_testcase_count,
    normalize_judge_output,
)
from execution_routing import get_submission_topic

logger = logging.getLogger(__name__)
_kafka_producer = None
KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092").split(",") if server.strip()]
AUTO_DISQUALIFY_SENTINEL = "DISQUALIFIED_AUTO_SUBMIT"
MAX_SUBMISSION_FILES = 32
MAX_FILE_PATH_LENGTH = 180
LANGUAGE_ENTRY_FILES = {
    'python': 'main.py',
    'javascript': 'main.js',
    'ruby': 'main.rb',
    'php': 'main.php',
    'cpp': 'main.cpp',
    'c': 'main.c',
    'go': 'main.go',
    'rust': 'main.rs',
    'java': 'Main.java',
    'typescript': 'main.ts',
    'sql': 'query.sql',
}
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '').strip()
GITHUB_CLIENT_ID = os.getenv('GITHUB_CLIENT_ID', '').strip()
GITHUB_CLIENT_SECRET = os.getenv('GITHUB_CLIENT_SECRET', '').strip()
GITHUB_REDIRECT_BASE = os.getenv('GITHUB_REDIRECT_BASE', '').strip()


def get_kafka_producer():
    global _kafka_producer
    if _kafka_producer is None:
        _kafka_producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
    return _kafka_producer


def parse_non_negative_int(value, default=0):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def get_participant_solved_question_ids(participant):
    assigned_question_ids = participant.assigned_questions.values_list('id', flat=True)
    solved_ids = Submission.objects.filter(
        room=participant.room,
        user=participant.student,
        question_id__in=assigned_question_ids,
        status='PASSED',
    ).values_list('question_id', flat=True).distinct()
    return list(solved_ids)


def lock_participant_access(participant):
    if participant.access_locked:
        return False

    participant.access_locked = True
    participant.access_locked_at = timezone.now()
    participant.save(update_fields=['access_locked', 'access_locked_at'])
    return True


def get_default_entry_file(language):
    return LANGUAGE_ENTRY_FILES.get(str(language or '').strip().lower(), 'main.txt')


def sanitize_relative_file_path(path_value):
    candidate = str(path_value or '').replace('\\', '/').strip().strip('/')
    if not candidate:
        return None
    if candidate.startswith('.') or candidate.startswith('~') or '//' in candidate:
        return None

    parts = [part for part in candidate.split('/') if part]
    if not parts or any(part in {'.', '..'} for part in parts):
        return None

    normalized = '/'.join(parts)
    if len(normalized) > MAX_FILE_PATH_LENGTH:
        return None
    return normalized


def normalize_submission_files(language, code, files_payload, entry_file=None):
    parsed_files = files_payload
    if isinstance(parsed_files, str):
        try:
            parsed_files = json.loads(parsed_files)
        except json.JSONDecodeError as exc:
            raise serializers.ValidationError({'files': f'Invalid files payload: {exc}'})

    if parsed_files in (None, ''):
        parsed_files = []

    if not isinstance(parsed_files, list):
        raise serializers.ValidationError({'files': 'Files payload must be an array of objects.'})

    normalized_files = []
    seen_paths = set()
    for raw_file in parsed_files[:MAX_SUBMISSION_FILES]:
        if not isinstance(raw_file, dict):
            raise serializers.ValidationError({'files': 'Each file entry must be an object.'})

        path = sanitize_relative_file_path(raw_file.get('path') or raw_file.get('name'))
        if not path:
            raise serializers.ValidationError({'files': 'Each file must use a safe relative path.'})

        content = str(raw_file.get('content') or '')
        if path in seen_paths:
            normalized_files = [file for file in normalized_files if file['path'] != path]
        normalized_files.append({'path': path, 'content': content})
        seen_paths.add(path)

    default_entry_file = get_default_entry_file(language)
    requested_entry_file = sanitize_relative_file_path(entry_file)

    if normalized_files:
        if requested_entry_file and requested_entry_file in seen_paths:
            resolved_entry_file = requested_entry_file
        elif default_entry_file in seen_paths:
            resolved_entry_file = default_entry_file
        else:
            resolved_entry_file = normalized_files[0]['path']
    else:
        resolved_entry_file = requested_entry_file or default_entry_file
        normalized_files.insert(0, {'path': resolved_entry_file, 'content': str(code or '')})
        seen_paths.add(resolved_entry_file)

    primary_file = next((file for file in normalized_files if file['path'] == resolved_entry_file), None)
    if primary_file is None:
        raise serializers.ValidationError({'entry_file': 'The selected entry file was not found in the workspace.'})

    return normalized_files, resolved_entry_file, primary_file['content']


def normalize_judge_cases_payload(judge_cases_payload):
    parsed_cases = judge_cases_payload
    if isinstance(parsed_cases, str):
        try:
            parsed_cases = json.loads(parsed_cases)
        except json.JSONDecodeError as exc:
            raise serializers.ValidationError({'judge_cases': f'Invalid testcase payload: {exc}'})

    if parsed_cases in (None, ''):
        return []

    if not isinstance(parsed_cases, list):
        raise serializers.ValidationError({'judge_cases': 'Visible testcases must be sent as an array.'})

    normalized_cases = []
    for raw_case in parsed_cases:
        if not isinstance(raw_case, dict):
            raise serializers.ValidationError({'judge_cases': 'Each testcase must be an object.'})
        normalized_cases.append({
            'input': str(raw_case.get('input') or ''),
            'expected_output': normalize_judge_output(raw_case.get('expected_output') or ''),
        })

    return normalized_cases


def build_provider_username_candidates(provider, provider_payload):
    provider = str(provider or '').strip().lower()
    candidates = []
    raw_candidates = [
        provider_payload.get('provider_username'),
        provider_payload.get('username'),
        provider_payload.get('login'),
        provider_payload.get('email', '').split('@')[0] if provider_payload.get('email') else '',
        provider_payload.get('name'),
        provider_payload.get('display_name'),
    ]

    for raw_value in raw_candidates:
        cleaned = slugify(str(raw_value or '').strip()).replace('-', '_')
        cleaned = re.sub(r'[^a-z0-9_]+', '', cleaned.lower())
        cleaned = cleaned.strip('_')
        if not cleaned:
            continue
        if cleaned[0].isdigit():
            cleaned = f'{provider}_{cleaned}'
        candidates.append(cleaned[:24])

    provider_prefix = f'{provider}_{provider_payload.get("provider_user_id", "")}'
    provider_prefix = slugify(provider_prefix).replace('-', '_').strip('_')
    if provider_prefix:
        candidates.append(provider_prefix[:24])

    candidates.append(f'{provider}_user')
    # Preserve order while removing duplicates
    return list(dict.fromkeys([candidate for candidate in candidates if candidate]))


def generate_unique_provider_username(provider, provider_payload):
    for base_candidate in build_provider_username_candidates(provider, provider_payload):
        if not User.objects.filter(username=base_candidate).exists():
            return base_candidate

        for suffix in range(2, 500):
            candidate = f'{base_candidate[:20]}_{suffix}'
            if not User.objects.filter(username=candidate).exists():
                return candidate

    fallback = f'{provider}_{secrets.token_hex(4)}'
    while User.objects.filter(username=fallback).exists():
        fallback = f'{provider}_{secrets.token_hex(4)}'
    return fallback


def get_or_create_social_user(provider, provider_payload, is_teacher=False):
    provider_user_id = str(provider_payload.get('provider_user_id') or '').strip()
    if not provider_user_id:
        raise serializers.ValidationError({'error': 'Missing provider user identifier.'})

    social_account = SocialAccount.objects.select_related('user').filter(
        provider=provider,
        provider_user_id=provider_user_id,
    ).first()

    if social_account:
        user = social_account.user
        social_account.provider_username = provider_payload.get('provider_username', social_account.provider_username or '')
        social_account.email = provider_payload.get('email', social_account.email or '')
        social_account.avatar_url = provider_payload.get('avatar_url', social_account.avatar_url or '')
        social_account.extra_data = provider_payload
        social_account.save(update_fields=['provider_username', 'email', 'avatar_url', 'extra_data'])
        profile, _ = UserProfile.objects.get_or_create(user=user, defaults={'is_teacher': is_teacher})
        return user, profile, False

    email = str(provider_payload.get('email') or '').strip().lower()
    user = None
    if email:
        user = User.objects.filter(email__iexact=email).first()

    created = False
    if user is None:
        username = generate_unique_provider_username(provider, provider_payload)
        user = User.objects.create_user(
            username=username,
            email=email or '',
            password=secrets.token_urlsafe(24),
        )
        created = True

    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={'is_teacher': is_teacher})

    SocialAccount.objects.create(
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_username=provider_payload.get('provider_username', ''),
        email=email,
        avatar_url=provider_payload.get('avatar_url', ''),
        extra_data=provider_payload,
    )

    return user, profile, created


def get_social_auth_config(request):
    redirect_base = GITHUB_REDIRECT_BASE or (request.build_absolute_uri('/login/').rstrip('/') if request is not None else '')
    return {
        'google_client_id': GOOGLE_CLIENT_ID,
        'github_client_id': GITHUB_CLIENT_ID,
        'github_redirect_uri': redirect_base,
        'enabled': {
            'google': bool(GOOGLE_CLIENT_ID),
            'github': bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET),
        }
    }


def verify_google_identity_token(id_token):
    if not GOOGLE_CLIENT_ID:
        raise serializers.ValidationError({'error': 'Google sign-in is not configured.'})

    response = requests.get(
        'https://oauth2.googleapis.com/tokeninfo',
        params={'id_token': id_token},
        timeout=8,
    )
    payload = response.json() if response.ok else {}
    if not response.ok:
        raise serializers.ValidationError({'error': payload.get('error_description') or 'Google token verification failed.'})

    aud = str(payload.get('aud') or '').strip()
    if aud != GOOGLE_CLIENT_ID:
        raise serializers.ValidationError({'error': 'Google token audience mismatch.'})

    return {
        'provider_user_id': str(payload.get('sub') or ''),
        'provider_username': payload.get('email', '').split('@')[0] if payload.get('email') else '',
        'email': payload.get('email', ''),
        'name': payload.get('name', ''),
        'avatar_url': payload.get('picture', ''),
        'email_verified': str(payload.get('email_verified', '')).lower() == 'true',
    }


def exchange_github_code_for_profile(code, redirect_uri):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise serializers.ValidationError({'error': 'GitHub sign-in is not configured.'})

    token_response = requests.post(
        'https://github.com/login/oauth/access_token',
        headers={'Accept': 'application/json'},
        data={
            'client_id': GITHUB_CLIENT_ID,
            'client_secret': GITHUB_CLIENT_SECRET,
            'code': code,
            'redirect_uri': redirect_uri or GITHUB_REDIRECT_BASE or '',
        },
        timeout=8,
    )
    token_payload = token_response.json() if token_response.ok else {}
    access_token = token_payload.get('access_token')
    if not access_token:
        raise serializers.ValidationError({'error': token_payload.get('error_description') or 'GitHub token exchange failed.'})

    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {access_token}',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    user_response = requests.get('https://api.github.com/user', headers=headers, timeout=8)
    user_payload = user_response.json() if user_response.ok else {}
    if not user_response.ok:
        raise serializers.ValidationError({'error': user_payload.get('message') or 'GitHub profile lookup failed.'})

    email = user_payload.get('email') or ''
    if not email:
        emails_response = requests.get('https://api.github.com/user/emails', headers=headers, timeout=8)
        if emails_response.ok:
            emails_payload = emails_response.json()
            primary_email = next((item.get('email') for item in emails_payload if item.get('primary') and item.get('verified')), None)
            fallback_email = next((item.get('email') for item in emails_payload if item.get('verified')), None)
            email = primary_email or fallback_email or ''

    return {
        'provider_user_id': str(user_payload.get('id') or ''),
        'provider_username': user_payload.get('login', ''),
        'email': email,
        'name': user_payload.get('name', ''),
        'avatar_url': user_payload.get('avatar_url', ''),
        'profile_url': user_payload.get('html_url', ''),
    }

# ==========================================
# 1. AUTHENTICATION ENDPOINTS
# ==========================================

class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return  # This effectively disables the CSRF check for this class

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def register_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    is_teacher = request.data.get('is_teacher', False)

    if not username or not password:
        return Response({'error': 'Username and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.filter(username=username).exists():
        return Response({'error': 'Username is already taken.'}, status=status.HTTP_400_BAD_REQUEST)

    user = User.objects.create_user(username=username, password=password)
    UserProfile.objects.create(user=user, is_teacher=is_teacher)
    token, _ = Token.objects.get_or_create(user=user)
    
    return Response({
        'token': token.key, 
        'user_id': user.id,
        'is_teacher': is_teacher,
        'message': 'Account created successfully'
    }, status=status.HTTP_201_CREATED)

@csrf_exempt
@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication, TokenAuthentication])
@permission_classes([AllowAny])
def login_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    user = authenticate(username=username, password=password)
    
    if user is not None:
        token, _ = Token.objects.get_or_create(user=user)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        
        return Response({
            'token': token.key, 
            'user_id': user.id, 
            'is_teacher': profile.is_teacher,
            'message': 'Login successful'
        }, status=status.HTTP_200_OK)
        
    return Response({'error': 'Invalid Credentials'}, status=status.HTTP_401_UNAUTHORIZED)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    request.user.auth_token.delete()
    return Response({'message': 'Successfully logged out'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def social_auth_config_view(request):
    return Response(get_social_auth_config(request), status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def google_social_login_view(request):
    id_token = request.data.get('id_token')
    is_teacher = parse_bool(request.data.get('is_teacher'), False)
    if not id_token:
        return Response({'error': 'Google identity token is required.'}, status=status.HTTP_400_BAD_REQUEST)

    provider_payload = verify_google_identity_token(id_token)
    user, profile, _ = get_or_create_social_user('google', provider_payload, is_teacher=is_teacher)
    token, _ = Token.objects.get_or_create(user=user)

    return Response({
        'token': token.key,
        'user_id': user.id,
        'username': user.username,
        'is_teacher': profile.is_teacher,
        'provider': 'google',
        'message': 'Google login successful',
    }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def github_social_login_view(request):
    code = request.data.get('code')
    redirect_uri = request.data.get('redirect_uri')
    is_teacher = parse_bool(request.data.get('is_teacher'), False)

    if not code:
        return Response({'error': 'GitHub authorization code is required.'}, status=status.HTTP_400_BAD_REQUEST)

    provider_payload = exchange_github_code_for_profile(code, redirect_uri)
    user, profile, _ = get_or_create_social_user('github', provider_payload, is_teacher=is_teacher)
    token, _ = Token.objects.get_or_create(user=user)

    return Response({
        'token': token.key,
        'user_id': user.id,
        'username': user.username,
        'is_teacher': profile.is_teacher,
        'provider': 'github',
        'message': 'GitHub login successful',
    }, status=status.HTTP_200_OK)


# ==========================================
# 2. TEACHER EXAM MANAGEMENT ENDPOINTS
# ==========================================

class ExamRoomListCreateView(generics.ListCreateAPIView):
    """Allows teachers to create rooms and view their active rooms."""
    serializer_class = ExamRoomSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Only return rooms created by this specific teacher
        return ExamRoom.objects.filter(teacher=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        # Automatically set the creator to the logged-in teacher
        serializer.save(teacher=self.request.user)


class ExamQuestionListCreateView(generics.ListCreateAPIView):
    """Allows teachers to add questions to a specific room."""
    serializer_class = ExamQuestionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        room_id = self.kwargs['room_id']
        return ExamQuestion.objects.filter(room_id=room_id, room__teacher=self.request.user)

    def perform_create(self, serializer):
        room_id = self.kwargs['room_id']
        try:
            room = ExamRoom.objects.get(id=room_id, teacher=self.request.user)
            serializer.save(room=room)
        except ExamRoom.DoesNotExist:
            raise serializers.ValidationError("Room not found or you don't have permission.")


class RoomParticipantListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        participants = RoomParticipant.objects.filter(room=room).select_related('student').order_by('student__username')
        
        data = []
        for p in participants:
            data.append({
                "student_id": p.student.id,
                "username": p.student.username,
                "joined_at": p.joined_at.isoformat() if p.joined_at else None,
                "access_locked": p.access_locked,
                "access_locked_at": p.access_locked_at.isoformat() if p.access_locked_at else None,
            })
        return Response(data)

class RoomSubmissionsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        submissions = Submission.objects.filter(room=room).select_related('user', 'question').order_by('-submitted_at')
        
        data = []
        for sub in submissions:
            data.append({
                "id": sub.id,
                "student_id": sub.user_id,
                "student_name": sub.user.username,
                "question_id": sub.question_id,
                "question_title": sub.question.title if sub.question else None,
                "status": sub.status,
                "total_score": sub.awarded_marks,
                "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
                "language": sub.language,
                "code": sub.code,
                "files": sub.files,
                "entry_file": sub.entry_file,
                "output": sub.output,
                "execution_time_ms": sub.execution_time_ms,
                "passed_testcases": sub.passed_testcases,
                "total_testcases": sub.total_testcases,
            })
        return Response(data)

class RoomQuestionDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, room_id, q_id):
        question = get_object_or_404(ExamQuestion, id=q_id, room_id=room_id, room__teacher=request.user)
        serializer = ExamQuestionSerializer(question, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, room_id, q_id):
        question = get_object_or_404(ExamQuestion, id=q_id, room_id=room_id, room__teacher=request.user)
        question.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class RoomDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        serializer = ExamRoomSerializer(room)
        return Response(serializer.data)

    def patch(self, request, pk):
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        serializer = ExamRoomSerializer(room, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        # Security: Only the teacher who created the room can delete it
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        room.delete()
        return Response(
            {"message": "Exam Room and all related data deleted successfully."}, 
            status=status.HTTP_200_OK
        )

class RoomParticipantDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, room_id, student_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        participant = get_object_or_404(RoomParticipant, room=room, student_id=student_id)
        did_lock = lock_participant_access(participant)

        if did_lock:
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f'user_{student_id}',
                    {
                        'type': 'send_submission_update', 
                        'data': {
                            'action': 'KICKED',
                            'room_id': room.id
                        }
                    }
                )
            except Exception as ws_error:
                logger.error(f"WebSocket Kick Broadcast Failed: {ws_error}")

        return Response({'message': 'Student blocked successfully.'}, status=status.HTTP_200_OK)

    def patch(self, request, room_id, student_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        participant = get_object_or_404(RoomParticipant, room=room, student_id=student_id)
        raw_value = request.data.get('access_locked')
        should_lock = str(raw_value).lower() in {'1', 'true', 'yes', 'on'}

        if should_lock:
            lock_participant_access(participant)
            return Response({'message': 'Student blocked successfully.'}, status=status.HTTP_200_OK)

        participant.access_locked = False
        participant.access_locked_at = None
        participant.save(update_fields=['access_locked', 'access_locked_at'])
        return Response({'message': 'Student unblocked successfully.'}, status=status.HTTP_200_OK)


class StudentRoomLockView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, room_id):
        participant = get_object_or_404(
            RoomParticipant.objects.select_related('room'),
            room_id=room_id,
            student=request.user,
        )
        lock_participant_access(participant)
        return Response({'message': 'Exam room access locked.'}, status=status.HTTP_200_OK)


class StudentRoomStateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        participant = RoomParticipant.objects.select_related('room__teacher').prefetch_related('assigned_questions').filter(
            room_id=room_id,
            student=request.user,
        ).first()

        if participant is None:
            return Response({'error': 'You are not a participant in this exam room.'}, status=status.HTTP_404_NOT_FOUND)

        if participant.access_locked:
            return Response({'error': 'Your access to this exam room has been locked.'}, status=status.HTTP_403_FORBIDDEN)

        room = participant.room
        assigned_qs = participant.assigned_questions.all()

        return Response({
            'room_title': room.title,
            'room_id': room.id,
            'teacher_name': room.teacher.username,
            'start_time': room.start_time.isoformat() if getattr(room, 'start_time', None) else None,
            'deadline': room.join_deadline.isoformat(),
            'assigned_questions': StudentExamQuestionSerializer(assigned_qs, many=True).data,
            'solved_question_ids': get_participant_solved_question_ids(participant),
        }, status=status.HTTP_200_OK)


# ==========================================
# 3. STUDENT ENROLLMENT ENDPOINT
# ==========================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def join_room_view(request):
    """Allows a student to join a room via a code and randomly assigns questions."""
    room_code = request.data.get('room_code')
    
    try:
        room = ExamRoom.objects.get(room_code=room_code, is_active=True)
    except ExamRoom.DoesNotExist:
        return Response({'error': 'Invalid or inactive room code.'}, status=status.HTTP_404_NOT_FOUND)

    # Allow resuming the exam if they reload the page
    participant = RoomParticipant.objects.filter(room=room, student=request.user).first()
    if participant:
        if participant.access_locked:
            return Response({'error': 'Your access to this exam room has been locked.'}, status=status.HTTP_403_FORBIDDEN)
        assigned_qs = participant.assigned_questions.all()
        return Response({
            'message': 'Resumed room successfully!',
            'room_title': room.title,
            'room_id':room.id,
            'teacher_name': room.teacher.username, # 🟢 ADDED FOR WAITING ROOM
            'start_time': room.start_time.isoformat() if getattr(room, 'start_time', None) else None, # 🟢 ADDED FOR WAITING ROOM
            'deadline': room.join_deadline.isoformat(), 
            'assigned_questions': StudentExamQuestionSerializer(assigned_qs, many=True).data,
            'solved_question_ids': get_participant_solved_question_ids(participant),
        }, status=status.HTTP_200_OK)

    # Check the strict deadline!
    if timezone.now() > room.join_deadline:
        return Response({'error': 'The deadline to join this exam has passed.'}, status=status.HTTP_403_FORBIDDEN)

    # Check if they are already in the room
    if RoomParticipant.objects.filter(room=room, student=request.user).exists():
        return Response({'error': 'You have already joined this room.'}, status=status.HTTP_400_BAD_REQUEST)

    # Generate the random subset of questions
    all_questions = list(room.questions.all())
    if len(all_questions) < room.questions_to_assign:
        return Response({'error': 'The teacher has not added enough questions to the pool yet.'}, status=status.HTTP_400_BAD_REQUEST)

    assigned_qs = random.sample(all_questions, room.questions_to_assign)

    # Save the participant and their assigned questions
    participant = RoomParticipant.objects.create(room=room, student=request.user)
    participant.assigned_questions.set(assigned_qs)

    return Response({
        'message': 'Successfully joined room!',
        'room_title': room.title,
        'room_id':room.id,
        'teacher_name': room.teacher.username, # 🟢 ADDED FOR WAITING ROOM
        'start_time': room.start_time.isoformat() if getattr(room, 'start_time', None) else None, # 🟢 ADDED FOR WAITING ROOM
        'deadline': room.join_deadline.isoformat(), 
        'assigned_questions': StudentExamQuestionSerializer(assigned_qs, many=True).data,
        'solved_question_ids': [],
    }, status=status.HTTP_201_CREATED)

# ==========================================
# 4. SUBMISSION ENDPOINTS
# ==========================================

class SubmissionCreateView(generics.CreateAPIView):
    """Accepts code and manual input, then pushes to Kafka with Room/Question context."""
    throttle_classes = [DynamicQueueThrottle]
    queryset = Submission.objects.all()
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        user_custom_input = self.request.data.get('user_input', "")
        room_id = self.request.data.get('room_id') or None
        question_id = self.request.data.get('question_id') or None
        incoming_files = self.request.data.get('files')
        incoming_entry_file = self.request.data.get('entry_file') or ''
        participant = None
        question = None
        judge_cases = []
        is_auto_disqualification = user_custom_input == AUTO_DISQUALIFY_SENTINEL

        try:
            if room_id is not None:
                room_id = int(room_id)
            if question_id is not None:
                question_id = int(question_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError({
                "error": "Invalid room or question identifier."
            })

        # Distinguish between "Run Test" and "Final Submit"
        if question_id:
            question = get_object_or_404(ExamQuestion, id=question_id)
            if room_id is None:
                room_id = question.room_id
            elif question.room_id != room_id:
                raise serializers.ValidationError({
                    "error": "The selected question does not belong to this exam room."
                })
            judge_cases = build_hidden_testcases(question.testcase_input, question.expected_output)
            user_custom_input = question.testcase_input
        else:
            judge_cases = normalize_judge_cases_payload(self.request.data.get('judge_cases'))

        if room_id:
            participant = RoomParticipant.objects.filter(
                room_id=room_id,
                student=self.request.user
            ).first()

            if not participant:
                raise serializers.ValidationError({
                    "error": "Access Denied. You are not a registered participant of this exam room."
                })

            if participant.access_locked and not is_auto_disqualification:
                raise serializers.ValidationError({
                    "error": "Your access to this exam room has been locked."
                })

        if question and participant and not participant.assigned_questions.filter(id=question.id).exists():
            raise serializers.ValidationError({
                "error": "This question is not assigned to you for this exam."
            })

        normalized_files, resolved_entry_file, primary_code = normalize_submission_files(
            self.request.data.get('language'),
            self.request.data.get('code', ''),
            incoming_files,
            incoming_entry_file,
        )

        # Save the submission to the DB
        with transaction.atomic():
            submission = serializer.save(
                user=self.request.user,
                room_id=room_id,
                question_id=question_id,
                code=primary_code,
                files=normalized_files,
                entry_file=resolved_entry_file,
                passed_testcases=0,
                total_testcases=len(judge_cases) if judge_cases else 0
            )
            if participant and is_auto_disqualification:
                lock_participant_access(participant)
        
        # Push to Kafka for Native Execution
        producer = get_kafka_producer()
        
        message = {
            'submission_id': submission.id,
            'code': submission.code,
            'files': submission.files,
            'entry_file': submission.entry_file,
            'language': submission.language,
            'user_input': user_custom_input,
            'time_limit_ms': self.request.data.get('time_limit_ms', 10000), 
        }
        if judge_cases:
            message['judge_cases'] = judge_cases

        try:
            submission_topic = get_submission_topic(submission.language)
        except ValueError as exc:
            submission.delete()
            raise serializers.ValidationError({"error": str(exc)})

        producer.send(submission_topic, message)
        producer.flush()

class SubmissionUpdateView(APIView):
    permission_classes = [] 

    def patch(self, request, pk):
        try:
            submission = Submission.objects.get(pk=pk)
            
            executor_status = request.data.get('status', submission.status)
            executor_output = request.data.get('output', submission.output)
            raw_passed_testcases = request.data.get('passed_testcases')
            raw_total_testcases = request.data.get('total_testcases')
            passed_testcases = parse_non_negative_int(
                raw_passed_testcases,
                submission.passed_testcases
            )
            total_testcases = parse_non_negative_int(
                raw_total_testcases,
                submission.total_testcases
            )
            if total_testcases and passed_testcases > total_testcases:
                passed_testcases = total_testcases
            
            final_status = executor_status
            awarded_marks = 0
            
            if executor_status == "SUCCESS" and submission.question:
                if raw_passed_testcases is not None or raw_total_testcases is not None:
                    if total_testcases == 0:
                        total_testcases = max(
                            get_hidden_testcase_count(
                                submission.question.testcase_input,
                                submission.question.expected_output
                            ),
                            1
                        )
                    if passed_testcases == total_testcases:
                        final_status = "PASSED"
                        awarded_marks = submission.question.total_marks
                    else:
                        final_status = "WRONG_ANSWER"
                        awarded_marks = 0
                else:
                    expected = normalize_judge_output(submission.question.expected_output)
                    actual = normalize_judge_output(executor_output)
                    total_testcases = max(
                        total_testcases,
                        get_hidden_testcase_count(
                            submission.question.testcase_input,
                            submission.question.expected_output
                        ),
                        1
                    )

                    if expected == actual:
                        passed_testcases = total_testcases
                        final_status = "PASSED"
                        awarded_marks = submission.question.total_marks
                    else:
                        passed_testcases = 0
                        final_status = "WRONG_ANSWER"
                        awarded_marks = 0
            elif submission.question and total_testcases == 0:
                total_testcases = get_hidden_testcase_count(
                    submission.question.testcase_input,
                    submission.question.expected_output
                )

            # Update the Database
            submission.status = final_status
            submission.output = executor_output
            submission.awarded_marks = awarded_marks
            submission.passed_testcases = passed_testcases
            submission.total_testcases = total_testcases
            submission.execution_time_ms = request.data.get('execution_time_ms', submission.execution_time_ms)
            submission.save()

            # Broadcast to WebSockets
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f'user_{submission.user.id}',
                    {
                        'type': 'send_submission_update',
                        'data': {
                            'submission_id': submission.id,
                            'room_id': submission.room_id,
                            'question_id': submission.question_id,
                            'status': submission.status, 
                            'output': submission.output,
                            'execution_time': submission.execution_time_ms,
                            'awarded_marks': submission.awarded_marks,
                            'passed_testcases': submission.passed_testcases,
                            'total_testcases': submission.total_testcases
                        }
                    }
                )
            except Exception as ws_error:
                logger.error(f"WebSocket Broadcast Failed: {ws_error}")

            return Response({"message": "Updated successfully"}, status=status.HTTP_200_OK)

        except Submission.DoesNotExist:
            return Response({"error": "Submission not found"}, status=status.HTTP_404_NOT_FOUND)

class SubmissionListView(generics.ListAPIView):
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Submission.objects.filter(user=self.request.user).order_by('-submitted_at')

class SubmissionDetailView(generics.RetrieveAPIView):
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Submission.objects.filter(user=self.request.user)

class SubmissionDeleteView(generics.DestroyAPIView):
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Submission.objects.filter(user=self.request.user)
