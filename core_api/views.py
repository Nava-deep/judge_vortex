import json
import os
import random
import logging
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
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

from .models import *
from .serializers import *
from .throttles import DynamicQueueThrottle
from .judging import build_hidden_testcases, get_hidden_testcase_count, normalize_judge_output
from execution_routing import get_submission_topic

logger = logging.getLogger(__name__)
_kafka_producer = None
KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092").split(",") if server.strip()]
AUTO_DISQUALIFY_SENTINEL = "DISQUALIFIED_AUTO_SUBMIT"


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


def get_participant_solved_question_ids(participant):
    assigned_question_ids = participant.assigned_questions.values_list('id', flat=True)
    solved_ids = Submission.objects.filter(
        room=participant.room,
        user=participant.student,
        question_id__in=assigned_question_ids,
        status='PASSED',
    ).values_list('question_id', flat=True).distinct()
    return list(solved_ids)

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
        # select_related makes it highly efficient to grab the connected User model
        participants = RoomParticipant.objects.filter(room=room).select_related('student')
        
        data = []
        for p in participants:
            data.append({
                "student_id": p.student.id,
                "username": p.student.username, # 🟢 Grabs the actual string name!
                "joined_at": p.joined_at.isoformat() if p.joined_at else None
            })
        return Response(data)

class RoomSubmissionsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        submissions = Submission.objects.filter(room=room).select_related('user').order_by('-submitted_at')
        
        data = []
        for sub in submissions:
            data.append({
                "id": sub.id,
                "student_id": sub.user_id,
                "student_name": sub.user.username, # 🟢 Grabs the actual string name!
                "question_id": sub.question_id,
                "status": sub.status,
                "total_score": sub.awarded_marks,  # Ensures the score maps to the frontend
                "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None
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
            status=status.HTTP_204_NO_CONTENT
        )

class RoomParticipantDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, room_id, student_id):
        # 1. Verify the teacher owns this room
        room = get_object_or_404(ExamRoom, id=room_id, teacher=request.user)
        
        # 2. Find the participation record and delete it
        participant = get_object_or_404(RoomParticipant, room=room, student_id=student_id)
        participant.delete()
        
        # 3. Delete their submissions for this room
        Submission.objects.filter(room=room, user_id=student_id).delete()

        # 🟢 4. Broadcast an INSTANT KICK event via WebSockets to the student
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

        return Response(status=status.HTTP_204_NO_CONTENT)


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

    # 🟢 Allow resuming the exam if they reload the page
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

        # 🟢 Distinguish between "Run Test" and "Final Submit"
        if question_id:
            # If there is a question ID, it's a final submit. Override the stdin with hidden cases.
            question = get_object_or_404(ExamQuestion, id=question_id)
            if room_id is None:
                room_id = question.room_id
            elif question.room_id != room_id:
                raise serializers.ValidationError({
                    "error": "The selected question does not belong to this exam room."
                })
            judge_cases = build_hidden_testcases(question.testcase_input, question.expected_output)
            user_custom_input = question.testcase_input

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

        # Save the submission to the DB
        with transaction.atomic():
            submission = serializer.save(
                user=self.request.user,
                room_id=room_id,
                question_id=question_id,
                passed_testcases=0,
                total_testcases=len(judge_cases) if judge_cases else 0
            )
            if participant and is_auto_disqualification and not participant.access_locked:
                participant.access_locked = True
                participant.access_locked_at = timezone.now()
                participant.save(update_fields=['access_locked', 'access_locked_at'])
        
        # 📨 Push to Kafka for Native Execution
        producer = get_kafka_producer()
        
        message = {
            'submission_id': submission.id,
            'code': submission.code,
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
