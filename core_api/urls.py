from django.urls import path
from .views import (
    login_view, 
    logout_view, 
    register_view,
    update_theme,
    SubmissionUpdateView,
    SubmissionCreateView,
    SubmissionListView,
    SubmissionDeleteView,
)

urlpatterns = [
    # Auth Endpoints
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('register/', register_view, name='register'),
    path('theme/update/', update_theme, name='update-theme'),
    
    # Code Execution Endpoints
    path('submissions/', SubmissionListView.as_view(), name='submission-list'),
    path('submissions/submit/', SubmissionCreateView.as_view(), name='submit-code'),
    path('submissions/<int:pk>/update/', SubmissionUpdateView.as_view(), name='submission-update'),
    path('submissions/<int:pk>/delete/', SubmissionDeleteView.as_view(), name='submission-delete'),
]