from django.db import models
from django.contrib.auth.models import User

class Submission(models.Model):
    """The central transaction of the OJ. Links a User and their Code."""
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('EXECUTED', 'Executed'),
        ('TLE', 'Time Limit Exceeded'),
        ('MLE', 'Memory Limit Exceeded'),
        ('RUNTIME_ERROR', 'Runtime Error'),
        ('COMPILATION_ERROR', 'Compilation Error'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submissions')
    output = models.TextField(null=True, blank=True)
    code = models.TextField()
    language = models.CharField(max_length=50) # ✅ Removed choices constraint
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='PENDING')
    
    execution_time_ms = models.IntegerField(null=True, blank=True)
    memory_used_kb = models.IntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Sub #{self.id} | {self.user.username} | {self.language} | {self.status}"