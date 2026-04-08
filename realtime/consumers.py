import json
from channels.generic.websocket import AsyncWebsocketConsumer

from core_api.metrics import WEBSOCKET_DELIVERIES_TOTAL

class SubmissionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user_id = self.scope['url_route']['kwargs']['user_id']
        self.room_group_name = f'user_{self.user_id}'

        # Join the user's private room
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()
        WEBSOCKET_DELIVERIES_TOTAL.labels(event='connection', result='accepted').inc()

    async def disconnect(self, close_code):
        # Leave the room
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
        WEBSOCKET_DELIVERIES_TOTAL.labels(event='connection', result='closed').inc()

    # This method is called when we receive a message from Redis
    async def send_submission_update(self, event):
        # Send the actual data to the browser
        payload = dict(event['data'])
        payload.setdefault('schema_version', 1)
        payload.setdefault('event_type', 'submission.update')
        await self.send(text_data=json.dumps(payload))
        WEBSOCKET_DELIVERIES_TOTAL.labels(event=payload['event_type'], result='sent').inc()
