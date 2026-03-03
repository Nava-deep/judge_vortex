import json
import random
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework import status, generics
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from kafka import KafkaProducer
from rest_framework import serializers
import logging

from .models import Submission, UserProfile, ExamRoom, ExamQuestion, RoomParticipant
from .serializers import (
    SubmissionSerializer, ExamRoomSerializer, 
    ExamQuestionSerializer, RoomParticipantSerializer
)
from .throttles import DynamicQueueThrottle

logger = logging.getLogger(__name__)

# ==========================================
# 1. AUTHENTICATION ENDPOINTS
# ==========================================

@api_view(['POST'])
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

@api_view(['POST'])
@authentication_classes([])
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
        'assigned_questions': ExamQuestionSerializer(assigned_qs, many=True).data
    }, status=status.HTTP_201_CREATED)


# ==========================================
# 4. SUBMISSION ENDPOINTS (Unchanged)
# ==========================================

class SubmissionCreateView(generics.CreateAPIView):
    """Accepts code and manual input, then pushes to Kafka with Room/Question context."""
    throttle_classes = [DynamicQueueThrottle]
    queryset = Submission.objects.all()
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        user_custom_input = self.request.data.get('user_input', "")
        room_id = self.request.data.get('room_id')
        question_id = self.request.data.get('question_id')
        if room_id:
            is_participant = RoomParticipant.objects.filter(
                room_id=room_id, 
                student=self.request.user
            ).exists()
            
            if not is_participant:
                raise serializers.ValidationError({
                    "error": "Access Denied. You are not a registered participant of this exam room."
                })

        # Save the submission to the DB
        submission = serializer.save(
            user=self.request.user,
            room_id=room_id,
            question_id=question_id
        )
        
        # 📨 Push to Kafka for Native Execution
        producer = KafkaProducer(
            bootstrap_servers=['localhost:9092'],
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        
        message = {
            'submission_id': submission.id,
            'code': submission.code,
            'language': submission.language,
            'user_input': user_custom_input,
            'time_limit_ms': self.request.data.get('time_limit_ms', 10000), 
        }
        
        producer.send('code_submissions', message)
        producer.flush()

class SubmissionUpdateView(APIView):
    permission_classes = [] 

    def patch(self, request, pk):
        try:
            submission = Submission.objects.get(pk=pk)
            
            executor_status = request.data.get('status', submission.status)
            executor_output = request.data.get('output', submission.output)
            
            final_status = executor_status
            awarded_marks = 0
            
            if executor_status == "SUCCESS" and submission.question:
                expected = submission.question.expected_output.strip()
                actual = executor_output.strip() if executor_output else ""
                
                if expected == actual:
                    final_status = "PASSED"
                    awarded_marks = submission.question.total_marks
                else:
                    final_status = "WRONG_ANSWER"
                    awarded_marks = 0

            # Update the Database
            submission.status = final_status
            submission.output = executor_output
            submission.awarded_marks = awarded_marks
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
                            'status': submission.status, 
                            'output': submission.output,
                            'execution_time': submission.execution_time_ms,
                            'awarded_marks': submission.awarded_marks
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

class SubmissionDeleteView(generics.DestroyAPIView):
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Submission.objects.filter(user=self.request.user)
    
class RoomParticipantListView(generics.ListDestroyAPIView):
    """Allows teachers to see all students in a room and delete (kick) them."""
    serializer_class = RoomParticipantSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        room_id = self.kwargs['room_id']
        # Ensure only the teacher of the room can see/delete participants
        return RoomParticipant.objects.filter(room_id=room_id, room__teacher=self.request.user)

class RoomSubmissionsListView(generics.ListAPIView):
    """Allows teachers to see all code submissions made within their room."""
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        room_id = self.kwargs['room_id']
        # Security: Ensure the teacher requesting the list actually owns the room
        return Submission.objects.filter(
            room_id=room_id, 
            room__teacher=self.request.user
        ).order_by('-submitted_at')