from django.urls import re_path
from realtime.consumers import SubmissionConsumer

websocket_urlpatterns = [
    # Ensure this matches your consumer class name
    re_path(r'ws/submissions/(?P<user_id>\w+)/$', SubmissionConsumer.as_asgi()),
]