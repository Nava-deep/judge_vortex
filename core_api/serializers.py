from rest_framework import serializers
# Import all the new models we created
from .models import Submission, ExamRoom, ExamQuestion, RoomParticipant

class ExamQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamQuestion
        # REMOVE 'total_questions' from this list below
        fields = ['id', 'title', 'description', 'testcase_input', 'expected_output', 'total_marks']


class ExamRoomSerializer(serializers.ModelSerializer):
    total_questions = serializers.SerializerMethodField()
    teacher_username = serializers.CharField(source='teacher.username', read_only=True)

    class Meta:
        model = ExamRoom
        fields = [
            'id', 'title', 'room_code', 'questions_to_assign', 
            'start_time', 'join_deadline', 'created_at', 
            'total_questions', 'teacher_username'
        ]
        read_only_fields = ['room_code', 'created_at']

    def get_total_questions(self, obj):
        return obj.questions.count()

# --- 3. Participant Serializer (Student Enrollment) ---
class RoomParticipantSerializer(serializers.ModelSerializer):
    student_username = serializers.ReadOnlyField(source='student.username')
    room_title = serializers.ReadOnlyField(source='room.title')
    
    # We nest the questions so the student knows exactly what they were assigned
    assigned_questions = ExamQuestionSerializer(many=True, read_only=True)
    
    class Meta:
        model = RoomParticipant
        fields = ['id', 'room', 'room_title', 'student', 'student_username', 'assigned_questions', 'joined_at']
        read_only_fields = ['student', 'assigned_questions', 'joined_at']

# --- 4. Submission Serializer (Consolidated & Fixed) ---
class SubmissionSerializer(serializers.ModelSerializer):
    username = serializers.ReadOnlyField(source='user.username')
    
    class Meta:
        model = Submission
        fields = [
            'id', 'user', 'username', 'room', 'question', 
            'code', 'language', 'status', 'output', 
            'execution_time_ms', 'submitted_at', 'awarded_marks'
        ]
        read_only_fields = ['status', 'output', 'execution_time_ms', 'user', 'awarded_marks']