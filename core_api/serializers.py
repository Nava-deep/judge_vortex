from rest_framework import serializers
from .models import Submission

class SubmissionSerializer(serializers.ModelSerializer):
    username = serializers.ReadOnlyField(source='user.username')
    class Meta:
        model = Submission
        fields = [
            'id', 'user', 'username', 
            'code', 'language', 'status', 'output', 'execution_time_ms', 
            'submitted_at'
        ]
        read_only_fields = ['status', 'output', 'execution_time_ms', 'user']