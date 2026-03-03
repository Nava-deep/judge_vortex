from django.urls import path
from .views import (
    RoomParticipantListView,
    RoomSubmissionsListView,
    login_view, 
    logout_view, 
    register_view,
    join_room_view,
    SubmissionUpdateView,
    SubmissionCreateView,
    SubmissionListView,
    SubmissionDeleteView,
    ExamRoomListCreateView,
    ExamQuestionListCreateView,
)

urlpatterns = [
    # 🔐 Authentication
    path('register/', register_view, name='register'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    
    # 👨‍🏫 Teacher: Room & Question Management
    path('rooms/', ExamRoomListCreateView.as_view(), name='room-list-create'),
    path('rooms/<int:room_id>/questions/', ExamQuestionListCreateView.as_view(), name='question-list-create'),
    path('rooms/<int:room_id>/participants/', RoomParticipantListView.as_view(), name='room-participants'),
    path('rooms/<int:room_id>/submissions/', RoomSubmissionsListView.as_view(), name='room-submissions'),

    # 🎓 Student: Exam & Execution
    path('rooms/join/', join_room_view, name='join-room'),
    path('submissions/submit/', SubmissionCreateView.as_view(), name='submit-code'),
    path('submissions/', SubmissionListView.as_view(), name='submission-list'),
    
    # ⚙️ Internal: Executor Callback
    path('submissions/<int:pk>/update/', SubmissionUpdateView.as_view(), name='submission-update'),
]