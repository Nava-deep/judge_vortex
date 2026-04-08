from rest_framework import serializers
# Import all the new models we created
from .models import Submission, ExamRoom, ExamQuestion, RoomParticipant, ExamEvent
from .judging import (
    build_hidden_testcases,
    build_visible_testcases,
    get_hidden_testcase_count,
    get_visible_testcase_count,
)

class ExamQuestionSerializer(serializers.ModelSerializer):
    testcase_count = serializers.SerializerMethodField()
    testcases = serializers.SerializerMethodField()
    visible_testcase_count = serializers.SerializerMethodField()
    visible_testcases = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = [
            'id',
            'room',
            'title',
            'description',
            'visible_testcase_input',
            'visible_expected_output',
            'testcase_input',
            'expected_output',
            'total_marks',
            'testcase_count',
            'testcases',
            'visible_testcase_count',
            'visible_testcases',
        ]
        read_only_fields = ['room']

    def get_testcase_count(self, obj):
        return get_hidden_testcase_count(obj.testcase_input, obj.expected_output)

    def get_testcases(self, obj):
        return build_hidden_testcases(obj.testcase_input, obj.expected_output)

    def get_visible_testcase_count(self, obj):
        return get_visible_testcase_count(obj.visible_testcase_input, obj.visible_expected_output)

    def get_visible_testcases(self, obj):
        return build_visible_testcases(obj.visible_testcase_input, obj.visible_expected_output)


class StudentExamQuestionSerializer(serializers.ModelSerializer):
    testcase_count = serializers.SerializerMethodField()
    visible_testcase_count = serializers.SerializerMethodField()
    visible_testcases = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = [
            'id',
            'title',
            'description',
            'total_marks',
            'testcase_count',
            'visible_testcase_count',
            'visible_testcases',
        ]

    def get_testcase_count(self, obj):
        return get_hidden_testcase_count(obj.testcase_input, obj.expected_output)

    def get_visible_testcase_count(self, obj):
        return get_visible_testcase_count(obj.visible_testcase_input, obj.visible_expected_output)

    def get_visible_testcases(self, obj):
        return build_visible_testcases(obj.visible_testcase_input, obj.visible_expected_output)


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

    def validate(self, attrs):
        start_time = attrs.get('start_time')
        join_deadline = attrs.get('join_deadline')
        questions_to_assign = attrs.get('questions_to_assign')

        if self.instance:
            if start_time is None:
                start_time = self.instance.start_time
            if join_deadline is None:
                join_deadline = self.instance.join_deadline
            if questions_to_assign is None:
                questions_to_assign = self.instance.questions_to_assign

        if questions_to_assign is not None and questions_to_assign < 1:
            raise serializers.ValidationError({
                'questions_to_assign': 'Questions to assign must be at least 1.'
            })

        if start_time and join_deadline and join_deadline <= start_time:
            raise serializers.ValidationError({
                'join_deadline': 'Exam end time must be later than the exam start time.'
            })

        return attrs

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
            'code', 'files', 'entry_file', 'language', 'status', 'output', 
            'execution_time_ms', 'submitted_at', 'awarded_marks',
            'passed_testcases', 'total_testcases'
        ]
        read_only_fields = ['status', 'output', 'execution_time_ms', 'user', 'awarded_marks', 'passed_testcases', 'total_testcases']


class ExamEventSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source='actor.username', read_only=True)
    participant_username = serializers.CharField(source='participant.username', read_only=True)

    class Meta:
        model = ExamEvent
        fields = [
            'id',
            'room',
            'question',
            'submission',
            'actor',
            'actor_username',
            'participant',
            'participant_username',
            'event_type',
            'severity',
            'message',
            'metadata',
            'created_at',
        ]
        read_only_fields = fields
