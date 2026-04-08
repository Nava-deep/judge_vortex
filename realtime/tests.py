from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings

from judge_vortex.asgi import application


@override_settings(
    CHANNEL_LAYERS={
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        }
    }
)
class SubmissionConsumerTests(TransactionTestCase):
    def test_consumer_receives_group_message_with_schema_defaults(self):
        async def scenario():
            communicator = WebsocketCommunicator(application, '/ws/submissions/42/')
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            channel_layer = get_channel_layer()
            await channel_layer.group_send(
                'user_42',
                {
                    'type': 'send_submission_update',
                    'data': {
                        'submission_id': 99,
                        'status': 'PASSED',
                    },
                },
            )

            response = await communicator.receive_json_from(timeout=3)
            self.assertEqual(response['submission_id'], 99)
            self.assertEqual(response['status'], 'PASSED')
            self.assertEqual(response['schema_version'], 1)
            self.assertEqual(response['event_type'], 'submission.update')

            await communicator.disconnect()

        async_to_sync(scenario)()
