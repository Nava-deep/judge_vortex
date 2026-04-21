import json
import os
import random
import logging
from urllib.parse import urlparse
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from rest_framework import status, generics
from rest_framework.exceptions import APIException
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

from .models import ExamEvent, ExamQuestion, ExamRoom, ExamWorkspaceSnapshot, RoomParticipant, Submission, UserProfile
from .serializers import (
    ExamEventSerializer,
    ExamQuestionSerializer,
    ExamRoomSerializer,
    StudentExamQuestionSerializer,
    SubmissionSerializer,
)
from .throttles import DynamicQueueThrottle
from .audit import record_exam_event
from .health import check_cache, check_database, check_tcp_dependency
from .metrics import (
    PARTICIPANT_LOCK_EVENTS_TOTAL,
    READINESS_CHECK_TOTAL,
    ROOM_JOINS_TOTAL,
    SUBMISSIONS_QUEUED_TOTAL,
    SUBMISSIONS_RECEIVED_TOTAL,
    SUBMISSION_END_TO_END_SECONDS,
    SUBMISSION_VERDICTS_TOTAL,
    WEBSOCKET_DELIVERIES_TOTAL,
)
from .suspicion import detect_suspicious_result_patterns, detect_suspicious_submission_patterns
from .judging import (
    build_hidden_testcases,
    get_hidden_testcase_count,
    normalize_judge_output,
)
from executor_service.sandbox import (
    execute_prepared,
    get_missing_runtime_tools,
    prepare_execution,
    run_code_in_sandbox,
)
from execution_routing import get_all_submission_topics, get_submission_topic

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
class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = 'Judge Vortex is temporarily unavailable.'
    default_code = 'service_unavailable'


def get_primary_kafka_dependency():
    bootstrap = KAFKA_BOOTSTRAP_SERVERS[0] if KAFKA_BOOTSTRAP_SERVERS else '127.0.0.1:9092'
    parsed = urlparse(f'//{bootstrap}')
    return parsed.hostname or '127.0.0.1', parsed.port or 9092


def emit_user_event(user_id, event_type, payload):
    event_payload = {
        'schema_version': 1,
        'event_type': event_type,
        **payload,
    }
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'user_{user_id}',
            {
                'type': 'send_submission_update',
                'data': event_payload,
            }
        )
        WEBSOCKET_DELIVERIES_TOTAL.labels(event=event_type, result='success').inc()
    except Exception as ws_error:
        WEBSOCKET_DELIVERIES_TOTAL.labels(event=event_type, result='failure').inc()
        logger.error('websocket.delivery_failed %s', json.dumps({
            'event_type': event_type,
            'user_id': user_id,
            'error': str(ws_error),
        }, sort_keys=True))


def log_submission_lifecycle(event_type, **payload):
    logger.info('submission.lifecycle %s', json.dumps({'event_type': event_type, **payload}, sort_keys=True, default=str))


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


PARTICIPANT_ACTIVE_WINDOW_SECONDS = parse_non_negative_int(
    os.getenv("PARTICIPANT_ACTIVE_WINDOW_SECONDS", 25),
    25,
) or 25


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
    participant.last_presence_at = None
    participant.save(update_fields=['access_locked', 'access_locked_at', 'last_presence_at'])
    return True


def mark_participant_present(participant, now=None):
    if participant is None:
        return

    now = now or timezone.now()
    if participant.last_presence_at and abs((now - participant.last_presence_at).total_seconds()) < 5:
        return

    participant.last_presence_at = now
    participant.save(update_fields=['last_presence_at'])


def participant_is_active(participant, now=None):
    if participant is None or participant.access_locked or not participant.last_presence_at:
        return False

    now = now or timezone.now()
    return (now - participant.last_presence_at).total_seconds() <= PARTICIPANT_ACTIVE_WINDOW_SECONDS


def upsert_workspace_snapshot(*, room, student, question, language, code, files, entry_file):
    snapshot, _ = ExamWorkspaceSnapshot.objects.update_or_create(
        room=room,
        student=student,
        question=question,
        defaults={
            'language': language,
            'code': code,
            'files': files,
            'entry_file': entry_file,
        },
    )
    return snapshot


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


def finalize_submission_result(submission, executor_status, executor_output, execution_time_ms, passed_testcases=None, total_testcases=None):
    passed_testcases = parse_non_negative_int(
        passed_testcases,
        submission.passed_testcases
    )
    total_testcases = parse_non_negative_int(
        total_testcases,
        submission.total_testcases
    )

    if total_testcases and passed_testcases > total_testcases:
        passed_testcases = total_testcases

    final_status = executor_status
    awarded_marks = 0

    if executor_status == "SUCCESS" and submission.question:
        if passed_testcases or total_testcases:
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

    submission.status = final_status
    submission.output = executor_output
    submission.awarded_marks = awarded_marks
    submission.passed_testcases = passed_testcases
    submission.total_testcases = total_testcases
    submission.execution_time_ms = execution_time_ms or submission.execution_time_ms
    submission.save()
    SUBMISSION_VERDICTS_TOTAL.labels(language=submission.language, status=submission.status).inc()
    if submission.submitted_at:
        latency_seconds = max((timezone.now() - submission.submitted_at).total_seconds(), 0.0)
        SUBMISSION_END_TO_END_SECONDS.labels(language=submission.language, status=submission.status).observe(latency_seconds)

    severity = 'info'
    if submission.status in {'WRONG_ANSWER', 'TLE', 'MLE', 'RUNTIME_ERROR'}:
        severity = 'warning'
    if submission.status in {'COMPILATION_ERROR', 'SYSTEM_ERROR'}:
        severity = 'error'

    record_exam_event(
        event_type='submission.updated',
        message=f'Submission {submission.id} finished with {submission.status}.',
        room=submission.room,
        question=submission.question,
        submission=submission,
        actor=submission.user,
        participant=submission.user,
        severity=severity,
        metadata={
            'executor_status': executor_status,
            'awarded_marks': submission.awarded_marks,
            'passed_testcases': submission.passed_testcases,
            'total_testcases': submission.total_testcases,
            'execution_time_ms': submission.execution_time_ms,
        },
    )
    log_submission_lifecycle(
        'submission.updated',
        submission_id=submission.id,
        room_id=submission.room_id,
        question_id=submission.question_id,
        language=submission.language,
        executor_status=executor_status,
        final_status=submission.status,
        execution_time_ms=submission.execution_time_ms,
        passed_testcases=submission.passed_testcases,
        total_testcases=submission.total_testcases,
    )
    detect_suspicious_result_patterns(submission)

    emit_user_event(submission.user.id, 'submission.update', {
        'submission_id': submission.id,
        'room_id': submission.room_id,
        'question_id': submission.question_id,
        'status': submission.status,
        'output': submission.output,
        'execution_time': submission.execution_time_ms,
        'awarded_marks': submission.awarded_marks,
        'passed_testcases': submission.passed_testcases,
        'total_testcases': submission.total_testcases,
    })

    return submission


def execute_submission_inline(submission, user_input, judge_cases, time_limit_ms):
    if judge_cases:
        total_testcases = len(judge_cases)
        passed_testcases = 0
        total_time_ms = 0
        last_output = ""

        prepared = async_to_sync(prepare_execution)(
            submission.code,
            submission.language,
            judge_cases[0].get('input', ''),
            files=submission.files,
            entry_file=submission.entry_file,
        )

        if isinstance(prepared, dict):
            return finalize_submission_result(
                submission,
                prepared['status'],
                prepared['output'],
                prepared.get('time_ms', 0),
                passed_testcases=0,
                total_testcases=total_testcases,
            )

        try:
            for case in judge_cases:
                result = async_to_sync(execute_prepared)(
                    prepared,
                    case.get('input', ''),
                    time_limit_ms,
                )
                total_time_ms += int(result.get('time_ms') or 0)
                last_output = result.get('output', '') or ''

                if result['status'] == 'TLE':
                    return finalize_submission_result(
                        submission,
                        'TLE',
                        'Time Limit Exceeded',
                        total_time_ms or time_limit_ms,
                        passed_testcases=passed_testcases,
                        total_testcases=total_testcases,
                    )

                if result['status'] in ['MEMORY_LIMIT_EXCEEDED', 'MLE']:
                    return finalize_submission_result(
                        submission,
                        'MLE',
                        result['output'],
                        total_time_ms,
                        passed_testcases=passed_testcases,
                        total_testcases=total_testcases,
                    )

                if result['status'] in ['RUNTIME_ERROR', 'COMPILATION_ERROR', 'SYSTEM_ERROR']:
                    return finalize_submission_result(
                        submission,
                        result['status'],
                        result['output'],
                        total_time_ms,
                        passed_testcases=passed_testcases,
                        total_testcases=total_testcases,
                    )

                actual_output = normalize_judge_output(result['output'])
                expected_output = normalize_judge_output(case.get('expected_output', ''))
                if actual_output == expected_output:
                    passed_testcases += 1
                    continue

                return finalize_submission_result(
                    submission,
                    'SUCCESS',
                    result['output'],
                    total_time_ms,
                    passed_testcases=passed_testcases,
                    total_testcases=total_testcases,
                )

            return finalize_submission_result(
                submission,
                'SUCCESS',
                last_output,
                total_time_ms,
                passed_testcases=passed_testcases,
                total_testcases=total_testcases,
            )
        finally:
            prepared.cleanup()

    result = async_to_sync(run_code_in_sandbox)(
        submission.code,
        submission.language,
        user_input,
        time_limit_ms,
        files=submission.files,
        entry_file=submission.entry_file,
    )

    if result['status'] == 'TLE':
        return finalize_submission_result(
            submission,
            'TLE',
            'Time Limit Exceeded',
            result.get('time_ms') or time_limit_ms,
        )
    if result['status'] in ['MEMORY_LIMIT_EXCEEDED', 'MLE']:
        return finalize_submission_result(
            submission,
            'MLE',
            result['output'],
            result.get('time_ms', 0),
        )
    if result['status'] in ['RUNTIME_ERROR', 'COMPILATION_ERROR', 'SYSTEM_ERROR']:
        return finalize_submission_result(
            submission,
            result['status'],
            result['output'],
            result.get('time_ms', 0),
        )
    return finalize_submission_result(
        submission,
        'SUCCESS',
        result['output'],
        result.get('time_ms', 0),
    )


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
        room = serializer.save(teacher=self.request.user)
        record_exam_event(
            event_type='room.created',
            message=f'Room {room.room_code} created.',
            room=room,
            actor=self.request.user,
            metadata={
                'title': room.title,
                'questions_to_assign': room.questions_to_assign,
                'start_time': room.start_time.isoformat() if room.start_time else None,
                'join_deadline': room.join_deadline.isoformat() if room.join_deadline else None,
            },
        )


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
            question = serializer.save(room=room)
            record_exam_event(
                event_type='question.created',
                message=f'Question {question.title} added to room {room.room_code}.',
                room=room,
                question=question,
                actor=self.request.user,
                metadata={
                    'total_marks': question.total_marks,
                    'hidden_testcases': get_hidden_testcase_count(question.testcase_input, question.expected_output),
                },
            )
        except ExamRoom.DoesNotExist:
            raise serializers.ValidationError("Room not found or you don't have permission.")


class RoomEventListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        events = ExamEvent.objects.filter(room=room).select_related('actor', 'participant', 'submission', 'question')[:200]
        serializer = ExamEventSerializer(events, many=True)
        return Response(serializer.data)


class LivenessView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'status': 'ok',
            'service': 'judge-vortex-web',
            'timestamp': timezone.now().isoformat(),
        })


class ReadinessView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        has_errors = False
        kafka_host, kafka_port = get_primary_kafka_dependency()

        for dependency, callback in (
            ('database', check_database),
            ('cache', check_cache),
            ('kafka', lambda: check_tcp_dependency(kafka_host, kafka_port)),
        ):
            try:
                checks[dependency] = callback()
                READINESS_CHECK_TOTAL.labels(dependency=dependency, result='ok').inc()
            except Exception as exc:
                has_errors = True
                READINESS_CHECK_TOTAL.labels(dependency=dependency, result='error').inc()
                checks[dependency] = {
                    'status': 'error',
                    'error': str(exc),
                }

        checks['submission_topics'] = {
            'status': 'ok',
            'topics': get_all_submission_topics(),
        }

        return Response(
            {
                'status': 'degraded' if has_errors else 'ok',
                'service': 'judge-vortex-web',
                'timestamp': timezone.now().isoformat(),
                'checks': checks,
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE if has_errors else status.HTTP_200_OK,
        )


class RoomParticipantListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        participants = RoomParticipant.objects.filter(room=room).select_related('student').prefetch_related('assigned_questions').order_by('student__username')
        now = timezone.now()

        data = []
        for p in participants:
            is_active = participant_is_active(p, now=now)
            if not p.access_locked and not is_active:
                continue
            data.append({
                "student_id": p.student.id,
                "username": p.student.username,
                "joined_at": p.joined_at.isoformat() if p.joined_at else None,
                "access_locked": p.access_locked,
                "access_locked_at": p.access_locked_at.isoformat() if p.access_locked_at else None,
                "is_active": is_active,
                "last_presence_at": p.last_presence_at.isoformat() if p.last_presence_at else None,
                "assigned_questions": [
                    {"id": question.id, "title": question.title}
                    for question in p.assigned_questions.all()
                ],
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


class RoomWorkspaceSnapshotListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        snapshots = ExamWorkspaceSnapshot.objects.filter(room=room).select_related('student', 'question').order_by('-updated_at')

        data = []
        for snapshot in snapshots:
            data.append({
                "id": snapshot.id,
                "student_id": snapshot.student_id,
                "student_name": snapshot.student.username,
                "question_id": snapshot.question_id,
                "question_title": snapshot.question.title,
                "language": snapshot.language,
                "code": snapshot.code,
                "files": snapshot.files,
                "entry_file": snapshot.entry_file,
                "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
                "source": "snapshot",
            })
        return Response(data)


class StudentWorkspaceSnapshotView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, room_id, question_id):
        participant = get_object_or_404(
            RoomParticipant.objects.select_related('room').prefetch_related('assigned_questions'),
            room_id=room_id,
            student=request.user,
        )

        if participant.access_locked:
            return Response({'error': 'Your access to this exam room has been locked.'}, status=status.HTTP_403_FORBIDDEN)

        question = get_object_or_404(ExamQuestion, id=question_id, room_id=room_id)
        if not participant.assigned_questions.filter(id=question.id).exists():
            return Response({'error': 'This question is not assigned to you for this exam.'}, status=status.HTTP_403_FORBIDDEN)

        normalized_files, resolved_entry_file, primary_code = normalize_submission_files(
            request.data.get('language'),
            request.data.get('code', ''),
            request.data.get('files'),
            request.data.get('entry_file') or '',
        )

        snapshot = upsert_workspace_snapshot(
            room=participant.room,
            student=request.user,
            question=question,
            language=request.data.get('language') or 'python',
            code=primary_code,
            files=normalized_files,
            entry_file=resolved_entry_file,
        )
        mark_participant_present(participant)
        return Response(
            {
                'id': snapshot.id,
                'updated_at': snapshot.updated_at.isoformat() if snapshot.updated_at else None,
            },
            status=status.HTTP_200_OK,
        )

class RoomQuestionDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, room_id, q_id):
        question = get_object_or_404(ExamQuestion, id=q_id, room_id=room_id, room__teacher=request.user)
        serializer = ExamQuestionSerializer(question, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_question = serializer.save()
        record_exam_event(
            event_type='question.updated',
            message=f'Question {updated_question.title} updated.',
            room=updated_question.room,
            question=updated_question,
            actor=request.user,
            metadata={
                'total_marks': updated_question.total_marks,
                'hidden_testcases': get_hidden_testcase_count(updated_question.testcase_input, updated_question.expected_output),
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, room_id, q_id):
        question = get_object_or_404(ExamQuestion, id=q_id, room_id=room_id, room__teacher=request.user)
        record_exam_event(
            event_type='question.deleted',
            message=f'Question {question.title} deleted.',
            room=question.room,
            question=question,
            actor=request.user,
        )
        question.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class RoomDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        serializer = ExamRoomSerializer(room)
        return Response(serializer.data)

    def patch(self, request, pk):
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        serializer = ExamRoomSerializer(room, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_room = serializer.save()
        record_exam_event(
            event_type='room.updated',
            message=f'Room {updated_room.room_code} updated.',
            room=updated_room,
            actor=request.user,
            metadata={
                'title': updated_room.title,
                'questions_to_assign': updated_room.questions_to_assign,
                'start_time': updated_room.start_time.isoformat() if updated_room.start_time else None,
                'join_deadline': updated_room.join_deadline.isoformat() if updated_room.join_deadline else None,
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        # Security: Only the teacher who created the room can delete it
        room = get_object_or_404(ExamRoom, pk=pk, teacher=request.user)
        record_exam_event(
            event_type='room.deleted',
            message=f'Room {room.room_code} deleted.',
            room=room,
            actor=request.user,
            severity='warning',
            metadata={'title': room.title},
        )
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
            PARTICIPANT_LOCK_EVENTS_TOTAL.labels(source='teacher', action='lock').inc()
            record_exam_event(
                event_type='participant.locked',
                message=f'{participant.student.username} was removed from room {room.room_code}.',
                room=room,
                actor=request.user,
                participant=participant.student,
                severity='warning',
                metadata={'reason': 'teacher_kick'},
            )
            emit_user_event(student_id, 'participant.kicked', {
                'action': 'KICKED',
                'room_id': room.id,
            })

        return Response({'message': 'Student blocked successfully.'}, status=status.HTTP_200_OK)

    def patch(self, request, room_id, student_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        participant = get_object_or_404(RoomParticipant, room=room, student_id=student_id)
        raw_value = request.data.get('access_locked')
        should_lock = str(raw_value).lower() in {'1', 'true', 'yes', 'on'}

        if should_lock:
            lock_participant_access(participant)
            PARTICIPANT_LOCK_EVENTS_TOTAL.labels(source='teacher', action='lock').inc()
            record_exam_event(
                event_type='participant.locked',
                message=f'{participant.student.username} was blocked in room {room.room_code}.',
                room=room,
                actor=request.user,
                participant=participant.student,
                severity='warning',
                metadata={'reason': 'teacher_toggle'},
            )
            return Response({'message': 'Student blocked successfully.'}, status=status.HTTP_200_OK)

        participant.access_locked = False
        participant.access_locked_at = None
        participant.last_presence_at = None
        participant.save(update_fields=['access_locked', 'access_locked_at', 'last_presence_at'])
        PARTICIPANT_LOCK_EVENTS_TOTAL.labels(source='teacher', action='unlock').inc()
        record_exam_event(
            event_type='participant.unlocked',
            message=f'{participant.student.username} was unblocked in room {room.room_code}.',
            room=room,
            actor=request.user,
            participant=participant.student,
            metadata={'reason': 'teacher_toggle'},
        )
        return Response({'message': 'Student unblocked successfully.'}, status=status.HTTP_200_OK)


class StudentRoomLockView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, room_id):
        participant = get_object_or_404(
            RoomParticipant.objects.select_related('room'),
            room_id=room_id,
            student=request.user,
        )
        if lock_participant_access(participant):
            PARTICIPANT_LOCK_EVENTS_TOTAL.labels(source='student_client', action='lock').inc()
            record_exam_event(
                event_type='participant.locked',
                message=f'{request.user.username} was locked out of room {participant.room.room_code}.',
                room=participant.room,
                actor=request.user,
                participant=request.user,
                severity='warning',
                metadata={'reason': 'self_lock_endpoint'},
            )
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

        mark_participant_present(participant)
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
        ROOM_JOINS_TOTAL.labels(result='invalid').inc()
        return Response({'error': 'Invalid or inactive room code.'}, status=status.HTTP_404_NOT_FOUND)

    # Allow resuming the exam if they reload the page
    participant = RoomParticipant.objects.filter(room=room, student=request.user).first()
    if participant:
        if participant.access_locked:
            ROOM_JOINS_TOTAL.labels(result='locked').inc()
            return Response({'error': 'Your access to this exam room has been locked.'}, status=status.HTTP_403_FORBIDDEN)
        mark_participant_present(participant)
        assigned_qs = participant.assigned_questions.all()
        ROOM_JOINS_TOTAL.labels(result='resumed').inc()
        record_exam_event(
            event_type='participant.resumed',
            message=f'{request.user.username} resumed room {room.room_code}.',
            room=room,
            actor=request.user,
            participant=request.user,
        )
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
        ROOM_JOINS_TOTAL.labels(result='deadline_blocked').inc()
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
    mark_participant_present(participant)
    ROOM_JOINS_TOTAL.labels(result='joined').inc()
    record_exam_event(
        event_type='participant.joined',
        message=f'{request.user.username} joined room {room.room_code}.',
        room=room,
        actor=request.user,
        participant=request.user,
        metadata={'assigned_question_ids': [question.id for question in assigned_qs]},
    )

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
        requested_time_limit_ms = parse_non_negative_int(self.request.data.get('time_limit_ms', 10000), 10000) or 10000
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

        if participant and not is_auto_disqualification:
            mark_participant_present(participant)

        normalized_files, resolved_entry_file, primary_code = normalize_submission_files(
            self.request.data.get('language'),
            self.request.data.get('code', ''),
            incoming_files,
            incoming_entry_file,
        )
        submission_mode = 'hidden' if question_id else 'visible'

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
                PARTICIPANT_LOCK_EVENTS_TOTAL.labels(source='auto_disqualify', action='lock').inc()
                record_exam_event(
                    event_type='participant.locked',
                    message=f'{self.request.user.username} was auto-locked after leaving the exam context.',
                    room=participant.room,
                    actor=self.request.user,
                    participant=self.request.user,
                    severity='warning',
                    metadata={'reason': 'auto_disqualify_submit'},
                )

        SUBMISSIONS_RECEIVED_TOTAL.labels(language=submission.language, mode=submission_mode).inc()
        if participant and question:
            upsert_workspace_snapshot(
                room=participant.room,
                student=self.request.user,
                question=question,
                language=submission.language,
                code=submission.code,
                files=submission.files,
                entry_file=submission.entry_file,
            )
        record_exam_event(
            event_type='submission.received',
            message=f'Submission {submission.id} received for {submission.language}.',
            room=submission.room,
            question=question,
            submission=submission,
            actor=self.request.user,
            participant=self.request.user,
            metadata={
                'language': submission.language,
                'mode': submission_mode,
                'entry_file': submission.entry_file,
                'workspace_file_count': len(submission.files or []),
                'judge_case_count': len(judge_cases),
            },
        )
        log_submission_lifecycle(
            'submission.received',
            submission_id=submission.id,
            user_id=self.request.user.id,
            room_id=submission.room_id,
            question_id=submission.question_id,
            language=submission.language,
            mode=submission_mode,
            entry_file=submission.entry_file,
            judge_case_count=len(judge_cases),
        )
        detect_suspicious_submission_patterns(submission)
        
        should_execute_inline = question_id is None and not get_missing_runtime_tools(submission.language)

        if should_execute_inline:
            execute_submission_inline(
                submission,
                user_custom_input,
                judge_cases,
                requested_time_limit_ms,
            )
            return

        producer = get_kafka_producer()
        message = {
            'submission_id': submission.id,
            'correlation_id': f'sub-{submission.id}',
            'delivery_attempt': 1,
            'code': submission.code,
            'files': submission.files,
            'entry_file': submission.entry_file,
            'language': submission.language,
            'user_input': user_custom_input,
            'time_limit_ms': requested_time_limit_ms,
        }
        if judge_cases:
            message['judge_cases'] = judge_cases

        try:
            submission_topic = get_submission_topic(submission.language)
        except ValueError as exc:
            submission.delete()
            raise serializers.ValidationError({"error": str(exc)})

        try:
            producer.send(submission_topic, message)
            producer.flush()
        except Exception as exc:
            submission.status = 'SYSTEM_ERROR'
            submission.output = 'Submission queue is temporarily unavailable.'
            submission.save(update_fields=['status', 'output'])
            record_exam_event(
                event_type='submission.queue_failed',
                message=f'Submission {submission.id} failed to enqueue.',
                room=submission.room,
                question=question,
                submission=submission,
                actor=self.request.user,
                participant=self.request.user,
                severity='error',
                metadata={'topic': submission_topic, 'error': str(exc)},
            )
            log_submission_lifecycle(
                'submission.queue_failed',
                submission_id=submission.id,
                language=submission.language,
                topic=submission_topic,
                error=str(exc),
            )
            raise ServiceUnavailable('Judging queue is temporarily unavailable. Please retry.')

        SUBMISSIONS_QUEUED_TOTAL.labels(language=submission.language, topic=submission_topic).inc()
        record_exam_event(
            event_type='submission.queued',
            message=f'Submission {submission.id} queued on {submission_topic}.',
            room=submission.room,
            question=question,
            submission=submission,
            actor=self.request.user,
            participant=self.request.user,
            metadata={'topic': submission_topic, 'mode': submission_mode},
        )
        log_submission_lifecycle(
            'submission.queued',
            submission_id=submission.id,
            language=submission.language,
            topic=submission_topic,
            mode=submission_mode,
        )

class SubmissionUpdateView(APIView):
    permission_classes = [] 

    def patch(self, request, pk):
        try:
            submission = Submission.objects.get(pk=pk)
            
            executor_status = request.data.get('status', submission.status)
            executor_output = request.data.get('output', submission.output)
            raw_passed_testcases = request.data.get('passed_testcases')
            raw_total_testcases = request.data.get('total_testcases')
            finalize_submission_result(
                submission,
                executor_status,
                executor_output,
                parse_non_negative_int(request.data.get('execution_time_ms'), submission.execution_time_ms),
                passed_testcases=raw_passed_testcases,
                total_testcases=raw_total_testcases,
            )

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
