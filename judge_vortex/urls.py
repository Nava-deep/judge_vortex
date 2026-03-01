from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render, redirect  # <-- Add 'redirect' here

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
    path('', home_redirect, name='home'),
    
    path('admin/', admin.site.urls),
    path('api/', include('core_api.urls')),
    
    # Frontend Routes
    path('login/', login_page, name='login_page'),
    path('register/', register_page, name='register_page'),
    path('workspace/', workspace_page, name='workspace_page'),
]