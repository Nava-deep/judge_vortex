from django.contrib import admin

from .models import ExamEvent, ExamQuestion, ExamRoom, ExamWorkspaceSnapshot, RoomParticipant, SocialAccount, Submission, UserProfile


@admin.register(ExamRoom)
class ExamRoomAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'room_code', 'teacher', 'questions_to_assign', 'start_time', 'join_deadline', 'is_active')
    list_filter = ('is_active', 'created_at')
    search_fields = ('title', 'room_code', 'teacher__username')
    ordering = ('-created_at',)


@admin.register(ExamQuestion)
class ExamQuestionAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'room', 'total_marks')
    list_filter = ('room',)
    search_fields = ('title', 'room__title', 'room__room_code')


@admin.register(RoomParticipant)
class RoomParticipantAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'student', 'access_locked', 'access_locked_at', 'last_presence_at', 'joined_at')
    list_filter = ('access_locked', 'room')
    search_fields = ('room__title', 'room__room_code', 'student__username')
    filter_horizontal = ('assigned_questions',)


@admin.register(ExamWorkspaceSnapshot)
class ExamWorkspaceSnapshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'student', 'question', 'language', 'updated_at')
    list_filter = ('room', 'language')
    search_fields = ('room__title', 'room__room_code', 'student__username', 'question__title')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-updated_at',)


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'room',
        'question',
        'language',
        'status',
        'awarded_marks',
        'passed_testcases',
        'total_testcases',
        'execution_time_ms',
        'submitted_at',
    )
    list_filter = ('status', 'language', 'room')
    search_fields = ('user__username', 'room__room_code', 'question__title')
    readonly_fields = ('submitted_at',)
    ordering = ('-submitted_at',)


@admin.register(ExamEvent)
class ExamEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_type', 'severity', 'room', 'actor', 'participant', 'submission', 'created_at')
    list_filter = ('severity', 'event_type', 'room')
    search_fields = ('message', 'event_type', 'room__room_code', 'actor__username', 'participant__username')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ('id', 'provider', 'provider_user_id', 'provider_username', 'user', 'email', 'linked_at')
    list_filter = ('provider',)
    search_fields = ('provider_user_id', 'provider_username', 'user__username', 'email')
    readonly_fields = ('linked_at',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'is_teacher')
    list_filter = ('is_teacher',)
    search_fields = ('user__username', 'user__email')
