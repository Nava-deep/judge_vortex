import json
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
import logging

# 🟢 IMPORT THE NEW MODELS
from .models import Submission, UserProfile, ExamRoom, ExamQuestion
from .serializers import SubmissionSerializer
from .throttles import DynamicQueueThrottle

logger = logging.getLogger(__name__)

# --- Authentication Endpoints ---

@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    # 🟢 Feature 4: Catch the teacher checkbox (Defaults to Student if missing)
    is_teacher = request.data.get('is_teacher', False)

    if not username or not password:
        return Response({'error': 'Username and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.filter(username=username).exists():
        return Response({'error': 'Username is already taken.'}, status=status.HTTP_400_BAD_REQUEST)

    # 1. Create the User account
    user = User.objects.create_user(username=username, password=password)
    
    # 2. 🟢 Create the Profile and lock in their Role
    UserProfile.objects.create(user=user, is_teacher=is_teacher)
    
    # 3. Generate Auth Token
    token, _ = Token.objects.get_or_create(user=user)
    
    return Response({
        'token': token.key, 
        'user_id': user.id,
        'is_teacher': is_teacher, # 🟢 Tell frontend where to route them
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
        
        # 🟢 Safely check their role so the frontend knows if they are a Teacher or Student
        profile, _ = UserProfile.objects.get_or_create(user=user)
        
        return Response({
            'token': token.key, 
            'user_id': user.id, 
            'is_teacher': profile.is_teacher, # 🟢 Crucial for UI routing
            'message': 'Login successful'
        }, status=status.HTTP_200_OK)
        
    return Response({'error': 'Invalid Credentials'}, status=status.HTTP_401_UNAUTHORIZED)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    request.user.auth_token.delete()
    return Response({'message': 'Successfully logged out'}, status=status.HTTP_200_OK)

# --- Submission Endpoints ---

class SubmissionCreateView(generics.CreateAPIView):
    """Accepts code and manual input, then pushes to Kafka."""
    throttle_classes = [DynamicQueueThrottle]
    queryset = Submission.objects.all()
    serializer_class = SubmissionSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        user_custom_input = self.request.data.get('user_input', "")
        
        # 🟢 Feature 2: Catch Room and Question IDs if this is an Exam submission
        room_id = self.request.data.get('room_id')
        question_id = self.request.data.get('question_id')
        
        # Save the submission to the DB linked to the user (and room/question if present)
        submission = serializer.save(
            user=self.request.user,
            room_id=room_id,
            question_id=question_id
        )
        
        # Connect to Kafka
        producer = KafkaProducer(
            bootstrap_servers=['localhost:9092'],
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        
        # Build the message for the Executor
        message = {
            'submission_id': submission.id,
            'code': submission.code,
            'language': submission.language,
            'user_input': user_custom_input,
            'time_limit_ms': self.request.data.get('time_limit_ms', 10000), 
        }
        
        # Send it to the queue!
        producer.send('code_submissions', message)
        producer.flush()


class SubmissionUpdateView(APIView):
    """Endpoint for the Executor to update submission results."""
    permission_classes = [] 

    def patch(self, request, pk):
        try:
            submission = Submission.objects.get(pk=pk)
            
            # 1. Update the Database
            submission.status = request.data.get('status', submission.status)
            submission.output = request.data.get('output', submission.output)
            submission.execution_time_ms = request.data.get('execution_time_ms', submission.execution_time_ms)
            submission.save()

            # 2. Safely Broadcast to WebSockets
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
                            'execution_time': submission.execution_time_ms
                        }
                    }
                )
            except Exception as ws_error:
                logger.error(f"WebSocket Broadcast Failed for Sub {pk}: {ws_error}")

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