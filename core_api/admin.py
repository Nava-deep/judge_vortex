from django.contrib import admin
from .models import *

admin.site.register(Submission)
admin.site.register(RoomParticipant)
admin.site.register(ExamQuestion)
admin.site.register(ExamRoom)
admin.site.register(UserProfile)