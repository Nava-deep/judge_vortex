import os
import django
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'judge_vortex.settings')
django.setup() # Initialize Django before importing routing

# Import these AFTER django.setup()
from channels.routing import ProtocolTypeRouter, URLRouter
from realtime.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter(
        websocket_urlpatterns
    ),
})