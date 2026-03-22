from django.urls import path
from .views import (
    login_view, 
    logout_view, 
    register_view,
    join_room_view,
    RoomParticipantDeleteView,
    RoomDetailView,
    RoomParticipantListView,
    RoomQuestionDeleteView,
    RoomSubmissionsListView,
    SubmissionUpdateView,
    SubmissionCreateView,
    SubmissionDetailView,
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
    path('rooms/<int:room_id>/questions/<int:q_id>/', RoomQuestionDeleteView.as_view(), name='delete-question'),
    path('rooms/<int:pk>/', RoomDetailView.as_view(), name='room-detail'),
    path('rooms/<int:room_id>/participants/<int:student_id>/', RoomParticipantDeleteView.as_view(), name='kick-student'),

    # 🎓 Student: Exam & Execution
    path('rooms/join/', join_room_view, name='join-room'),
    path('submissions/submit/', SubmissionCreateView.as_view(), name='submit-code'),
    path('submissions/', SubmissionListView.as_view(), name='submission-list'),
    path('submissions/<int:pk>/', SubmissionDetailView.as_view(), name='submission-detail'),
    path('submissions/<int:pk>/delete/', SubmissionDeleteView.as_view(), name='submission-delete'),
    
    # ⚙️ Internal: Executor Callback
    path('submissions/<int:pk>/update/', SubmissionUpdateView.as_view(), name='submission-update'),
]
