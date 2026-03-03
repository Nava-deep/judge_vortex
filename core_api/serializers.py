from rest_framework import serializers
# Import all the new models we created
from .models import Submission, ExamRoom, ExamQuestion, RoomParticipant

# --- 1. Question Serializer ---
class ExamQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamQuestion
        fields = ['id', 'room', 'title', 'description', 'testcase_input', 'expected_output']
        read_only_fields = ['room'] # The view will automatically link the room

# --- 2. Room Serializer ---
class ExamRoomSerializer(serializers.ModelSerializer):
    teacher_username = serializers.ReadOnlyField(source='teacher.username')
    # Automatically count how many questions the teacher has added to the pool
    total_questions = serializers.SerializerMethodField()
    
    class Meta:
        model = ExamRoom
        fields = [
            'id', 'teacher', 'teacher_username', 'room_code', 'title', 
            'join_deadline', 'questions_to_assign', 'is_active', 
            'created_at', 'total_questions'
        ]
        read_only_fields = ['teacher', 'room_code', 'is_active', 'created_at']

    def get_total_questions(self, obj):
        return obj.questions.count()

# --- 3. Participant Serializer (Student Enrollment) ---
class RoomParticipantSerializer(serializers.ModelSerializer):
    student_username = serializers.ReadOnlyField(source='student.username')
    room_title = serializers.ReadOnlyField(source='room.title')
    
    # We can nest the questions so the student knows exactly what they were assigned!
    assigned_questions = ExamQuestionSerializer(many=True, read_only=True)
    
    class Meta:
        model = RoomParticipant
        fields = ['id', 'room', 'room_title', 'student', 'student_username', 'assigned_questions', 'joined_at']
        read_only_fields = ['student', 'assigned_questions', 'joined_at']

# --- 4. Submission Serializer (Updated) ---
class SubmissionSerializer(serializers.ModelSerializer):
    username = serializers.ReadOnlyField(source='user.username')
    
    class Meta:
        model = Submission
        fields = [
            'id', 'user', 'username', 
            # NEW: Allow the frontend to submit which room and question this belongs to
            'room', 'question', 
            'code', 'language', 'status', 'output', 'execution_time_ms', 
            'submitted_at'
        ]
        read_only_fields = ['status', 'output', 'execution_time_ms', 'user']