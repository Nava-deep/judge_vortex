from django.db import models
from django.contrib.auth.models import User
import string
import random

def generate_room_code():
    """Generates a random 6-character uppercase alphanumeric code (e.g., A7X9P2)"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# --- 1. User Roles ---
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    is_teacher = models.BooleanField(default=False) # True = Teacher, False = Student

    def __str__(self):
        role = "Teacher" if self.is_teacher else "Student"
        return f"{self.user.username} - {role}"

# --- 2. Exam Management ---
class ExamRoom(models.Model):
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_rooms')
    room_code = models.CharField(max_length=10, unique=True, default=generate_room_code)
    title = models.CharField(max_length=100)
    start_time = models.DateTimeField(null=True, blank=True) # 🟢 ADD THIS
    # Time limit to join (Students cannot join after this exact time)
    join_deadline = models.DateTimeField() 
    
    # How many random questions each student gets from the pool
    questions_to_assign = models.IntegerField(default=3) 
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} [Code: {self.room_code}]"

class ExamQuestion(models.Model):
    room = models.ForeignKey(ExamRoom, on_delete=models.CASCADE, related_name='questions')
    title = models.CharField(max_length=100)
    description = models.TextField()
    total_marks = models.IntegerField(default=10)
    # Test cases for automated grading
    testcase_input = models.TextField(blank=True, default="")
    expected_output = models.TextField()

    def __str__(self):
        return f"[{self.room.room_code}] {self.title}"

# --- 3. Student Enrollment ---
class RoomParticipant(models.Model):
    """Tracks which students are in the room, and the random questions they must solve."""
    room = models.ForeignKey(ExamRoom, on_delete=models.CASCADE, related_name='participants')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='enrolled_rooms')
    
    # The randomly assigned subset of questions for this specific student
    assigned_questions = models.ManyToManyField(ExamQuestion)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A student can only join a specific room once!
        unique_together = ('room', 'student') 

    def __str__(self):
        return f"{self.student.username} in {self.room.title}"

# --- 4. Submissions (Updated) ---
class Submission(models.Model):
    """The central transaction of the OJ. Links a User, Room, Question, and their Code."""
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('PASSED', 'Passed'),
        ('WRONG_ANSWER', 'Wrong Answer'),
        ('EXECUTED', 'Executed'),
        ('TLE', 'Time Limit Exceeded'),
        ('MLE', 'Memory Limit Exceeded'),
        ('RUNTIME_ERROR', 'Runtime Error'),
        ('COMPILATION_ERROR', 'Compilation Error'),
        ('SYSTEM_ERROR', 'System Error'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submissions')
    
    # 🟢 NEW: Link the submission to the specific Exam and Question!
    room = models.ForeignKey(ExamRoom, on_delete=models.CASCADE, related_name='submissions', null=True, blank=True)
    question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, related_name='submissions', null=True, blank=True)
    awarded_marks = models.IntegerField(default=0)
    output = models.TextField(null=True, blank=True)
    code = models.TextField()
    language = models.CharField(max_length=50)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='PENDING')
    
    execution_time_ms = models.IntegerField(null=True, blank=True)
    memory_used_kb = models.IntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Sub #{self.id} | {self.user.username} | {self.status}"
