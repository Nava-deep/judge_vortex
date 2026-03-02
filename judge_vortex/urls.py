from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView

urlpatterns = [
    # 1. Background Metrics Scraper
    path('', include('django_prometheus.urls')),
    
    # 2. The Root Landing Page (Fallback if not asking for metrics)
    path('', TemplateView.as_view(template_name='index.html'), name='landing_page'),
    
    # 3. Backend Admin & APIs
    path('admin/', admin.site.urls),
    path('api/', include('core_api.urls')),
    
    # 4. Frontend Web Pages
    path('login/', TemplateView.as_view(template_name='login.html'), name='login_page'),
    path('register/', TemplateView.as_view(template_name='register.html'), name='register_page'),
    path('workspace/', TemplateView.as_view(template_name='workspace.html'), name='workspace_page'),
]