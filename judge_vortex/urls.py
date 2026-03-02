from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render, redirect
from django.views.generic import TemplateView, RedirectView

# --- Page Views ---
def login_page(request):
    return render(request, 'login.html')

def register_page(request):
    return render(request, 'register.html')

def workspace_page(request):
    return render(request, 'workspace.html')

# --- Root Redirect ---
def home_redirect(request):
    return redirect('login_page')  # Sends users from '/' straight to '/login/'

urlpatterns = [
    path('', include('django_prometheus.urls')),
    
    # Add the empty path ('') at the very top
    path('', TemplateView.as_view(template_name='index.html'), name='landing_page'),
    
    path('admin/', admin.site.urls),
    path('api/', include('core_api.urls')),
    
    # Frontend Routes
    path('login/', TemplateView.as_view(template_name='login.html'), name='login_page'),
    path('register/', TemplateView.as_view(template_name='register.html'), name='register_page'),
    path('workspace/', TemplateView.as_view(template_name='workspace.html'), name='workspace_page'),
]