from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from kafka import TopicPartition
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from . import views as core_views
from .integrations import ExternalRateLimitResult
from .judging import build_testcases
from .models import ExamEvent, ExamQuestion, ExamRoom, ExamWorkspaceSnapshot, RoomParticipant, Submission
from execution_routing import get_submission_topic


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class ExamQuestionApiTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher', password='pass123')
        self.token = Token.objects.create(user=self.teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Algorithms Midterm',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )

    def test_teacher_can_create_question_without_room_in_payload(self):
        response = self.client.post(
            f'/api/rooms/{self.room.id}/questions/',
            {
                'title': 'Two Sum',
                'description': 'Return indexes of the two numbers.',
                'testcase_input': '2 7 11 15\n9',
                'expected_output': '0 1',
                'total_marks': 10,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['room'], self.room.id)
        self.assertEqual(self.room.questions.count(), 1)

    @patch('core_api.models.generate_room_code', return_value='BBBBBB')
    def test_room_code_generation_retries_on_collision(self, mocked_generator):
        ExamRoom.objects.create(
            teacher=self.teacher,
            title='Existing Room',
            room_code='AAAAAA',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=2),
        )

        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='New Room',
            room_code='AAAAAA',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=2),
        )

        self.assertEqual(room.room_code, 'BBBBBB')
        self.assertEqual(mocked_generator.call_count, 1)

    def test_teacher_can_update_question_testcases(self):
        question = ExamQuestion.objects.create(
            room=self.room,
            title='Original',
            description='Original description',
            testcase_input='1',
            expected_output='1',
            total_marks=5,
        )

        response = self.client.patch(
            f'/api/rooms/{self.room.id}/questions/{question.id}/',
            {
                'title': 'Updated',
                'testcase_input': '2\n\n3',
                'expected_output': '4\n\n9',
                'total_marks': 15,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertEqual(question.title, 'Updated')
        self.assertEqual(question.total_marks, 15)
        self.assertEqual(response.data['testcase_count'], 2)
        self.assertEqual(len(response.data['testcases']), 2)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class StudentJoinRoomTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher-join', password='pass123')
        self.student = User.objects.create_user(username='student-join', password='pass123')
        self.token = Token.objects.create(user=self.student)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Hidden Judge Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        ExamQuestion.objects.create(
            room=self.room,
            title='Hidden Cases',
            description='Solve without seeing the hidden samples.',
            testcase_input='2\n\n3',
            expected_output='4\n\n9',
            total_marks=20,
        )

    def test_join_room_hides_hidden_io_and_returns_testcase_count(self):
        response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['assigned_questions']), 1)
        question = response.data['assigned_questions'][0]
        self.assertEqual(question['testcase_count'], 2)
        self.assertEqual(response.data['solved_question_ids'], [])
        self.assertNotIn('testcase_input', question)
        self.assertNotIn('expected_output', question)
        participant = RoomParticipant.objects.get(room=self.room, student=self.student)
        self.assertIsNotNone(participant.last_presence_at)

    def test_student_room_state_returns_live_exam_payload_without_hidden_io(self):
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

        state_response = self.client.get(f'/api/rooms/{self.room.id}/state/')
        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(state_response.data['room_id'], self.room.id)
        self.assertEqual(len(state_response.data['assigned_questions']), 1)
        question = state_response.data['assigned_questions'][0]
        self.assertEqual(question['testcase_count'], 2)
        self.assertNotIn('testcase_input', question)
        self.assertNotIn('expected_output', question)

    def test_join_and_room_state_restore_previously_solved_questions(self):
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

        question_id = join_response.data['assigned_questions'][0]['id']
        Submission.objects.create(
            user=self.student,
            room=self.room,
            question_id=question_id,
            code='print(4)',
            language='python',
            status='PASSED',
            awarded_marks=20,
        )

        rejoin_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(rejoin_response.status_code, 200)
        self.assertEqual(rejoin_response.data['solved_question_ids'], [question_id])

        state_response = self.client.get(f'/api/rooms/{self.room.id}/state/')
        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(state_response.data['solved_question_ids'], [question_id])

    @patch('core_api.views.KafkaProducer')
    def test_locked_participant_cannot_rejoin_after_auto_disqualification(self, kafka_producer_cls):
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

        assigned_question = join_response.data['assigned_questions'][0]
        submit_response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print(4)',
                'language': 'python',
                'user_input': core_views.AUTO_DISQUALIFY_SENTINEL,
                'room_id': self.room.id,
                'question_id': assigned_question['id'],
            },
            format='json',
        )

        self.assertEqual(submit_response.status_code, 201)
        participant = RoomParticipant.objects.get(room=self.room, student=self.student)
        self.assertTrue(participant.access_locked)
        self.assertIsNotNone(participant.access_locked_at)

        rejoin_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(rejoin_response.status_code, 403)
        self.assertEqual(rejoin_response.data['error'], 'Your access to this exam room has been locked.')

        state_response = self.client.get(f'/api/rooms/{self.room.id}/state/')
        self.assertEqual(state_response.status_code, 403)
        self.assertEqual(state_response.data['error'], 'Your access to this exam room has been locked.')

    def test_teacher_kick_locks_participant_and_blocks_rejoin(self):
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

        teacher_token = Token.objects.create(user=self.teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {teacher_token.key}')
        kick_response = self.client.delete(f'/api/rooms/{self.room.id}/participants/{self.student.id}/')
        self.assertEqual(kick_response.status_code, 200)

        participant = RoomParticipant.objects.get(room=self.room, student=self.student)
        self.assertTrue(participant.access_locked)
        self.assertIsNotNone(participant.access_locked_at)

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        rejoin_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(rejoin_response.status_code, 403)
        self.assertEqual(rejoin_response.data['error'], 'Your access to this exam room has been locked.')

    def test_student_room_lock_endpoint_blocks_rejoin_without_submission_context(self):
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

        lock_response = self.client.post(f'/api/rooms/{self.room.id}/lock/')
        self.assertEqual(lock_response.status_code, 200)

        participant = RoomParticipant.objects.get(room=self.room, student=self.student)
        self.assertTrue(participant.access_locked)
        self.assertIsNotNone(participant.access_locked_at)

        rejoin_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(rejoin_response.status_code, 403)
        self.assertEqual(rejoin_response.data['error'], 'Your access to this exam room has been locked.')


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    }
)
class SubmissionJudgingTests(APITestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='student', password='pass123')
        self.teacher = User.objects.create_user(username='teacher2', password='pass123')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Output Match Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Echo',
            description='Print the answer.',
            testcase_input='',
            expected_output='42\nDONE',
            total_marks=25,
        )
        self.submission = Submission.objects.create(
            user=self.student,
            room=self.room,
            question=self.question,
            code='print(42)',
            language='python',
            status='PROCESSING',
        )

    def test_submission_update_marks_question_passed_when_output_matches_expected(self):
        response = self.client.patch(
            f'/api/submissions/{self.submission.id}/update/',
            {
                'status': 'SUCCESS',
                'output': '42   \nDONE   \n',
                'execution_time_ms': 17,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, 'PASSED')
        self.assertEqual(self.submission.awarded_marks, self.question.total_marks)
        self.assertEqual(self.submission.execution_time_ms, 17)

    def test_submission_update_uses_testcase_counts_for_partial_failures(self):
        response = self.client.patch(
            f'/api/submissions/{self.submission.id}/update/',
            {
                'status': 'SUCCESS',
                'output': '41',
                'passed_testcases': 1,
                'total_testcases': 2,
                'execution_time_ms': 21,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, 'WRONG_ANSWER')
        self.assertEqual(self.submission.awarded_marks, 0)
        self.assertEqual(self.submission.passed_testcases, 1)
        self.assertEqual(self.submission.total_testcases, 2)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class RoomScheduleUpdateTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher3', password='pass123')
        self.token = Token.objects.create(user=self.teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Updated Schedule Room',
            questions_to_assign=2,
            start_time=timezone.now() + timedelta(days=1),
            join_deadline=timezone.now() + timedelta(days=2),
        )

    def test_teacher_can_update_room_schedule(self):
        new_start = timezone.now() + timedelta(days=3)
        new_end = timezone.now() + timedelta(days=4)

        response = self.client.patch(
            f'/api/rooms/{self.room.id}/',
            {
                'start_time': new_start.isoformat(),
                'join_deadline': new_end.isoformat(),
                'questions_to_assign': 4,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(response.data['id'], self.room.id)
        self.assertEqual(self.room.questions_to_assign, 4)
        self.assertEqual(self.room.start_time.isoformat().replace('+00:00', 'Z')[:16], new_start.isoformat().replace('+00:00', 'Z')[:16])
        self.assertEqual(self.room.join_deadline.isoformat().replace('+00:00', 'Z')[:16], new_end.isoformat().replace('+00:00', 'Z')[:16])


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class SubmissionCreateHiddenCasesTests(APITestCase):
    def setUp(self):
        core_views._kafka_producer = None
        self.teacher = User.objects.create_user(username='teacher-submit', password='pass123')
        self.student = User.objects.create_user(username='student-submit', password='pass123')
        self.token = Token.objects.create(user=self.student)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Multi Case Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Square',
            description='Print square.',
            testcase_input='2\n\n3',
            expected_output='4\n\n9',
            total_marks=10,
        )
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

    @patch('core_api.views.KafkaProducer')
    def test_final_submit_sets_hidden_testcase_total_and_pushes_cases(self, kafka_producer_cls):
        producer = kafka_producer_cls.return_value

        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print(4)',
                'language': 'python',
                'room_id': self.room.id,
                'question_id': self.question.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        submission = Submission.objects.get(id=response.data['id'])
        self.assertEqual(submission.total_testcases, 2)
        self.assertEqual(submission.passed_testcases, 0)
        producer.send.assert_called_once()
        topic, payload = producer.send.call_args[0]
        self.assertEqual(topic, get_submission_topic('python'))
        self.assertEqual(len(payload['judge_cases']), 2)

    @patch('core_api.views.KafkaProducer')
    def test_language_routes_to_matching_executor_topic(self, kafka_producer_cls):
        producer = kafka_producer_cls.return_value

        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'public class Main { public static void main(String[] args) { System.out.println(42); } }',
                'language': 'java',
                'room_id': self.room.id,
                'question_id': self.question.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        producer.send.assert_called_once()
        topic, payload = producer.send.call_args[0]
        self.assertEqual(topic, get_submission_topic('java'))
        self.assertEqual(payload['language'], 'java')

    @patch('core_api.views.KafkaProducer')
    def test_queue_failure_marks_submission_as_system_error_and_logs_event(self, kafka_producer_cls):
        producer = kafka_producer_cls.return_value
        producer.send.side_effect = RuntimeError('broker unavailable')

        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print(4)',
                'language': 'python',
                'room_id': self.room.id,
                'question_id': self.question.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 503)
        submission = Submission.objects.latest('id')
        self.assertEqual(submission.status, 'SYSTEM_ERROR')
        self.assertEqual(submission.output, 'Submission queue is temporarily unavailable.')
        self.assertTrue(
            ExamEvent.objects.filter(
                event_type='submission.queue_failed',
                submission=submission,
            ).exists()
        )

    @patch('core_api.views.KafkaProducer')
    def test_practice_run_executes_inline_without_kafka_and_returns_output(self, kafka_producer_cls):
        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'from hello import greet\nprint(greet())',
                'files': [
                    {'path': 'main.py', 'content': 'from hello import greet\nprint(greet())'},
                    {'path': 'hello.py', 'content': 'def greet():\n    return "practice ready"'},
                ],
                'entry_file': 'main.py',
                'language': 'python',
                'room_id': self.room.id,
                'judge_cases': [
                    {
                        'input': '',
                        'expected_output': 'practice ready',
                    }
                ],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'SUCCESS')
        self.assertEqual(response.data['output'].strip(), 'practice ready')
        self.assertEqual(response.data['passed_testcases'], 1)
        self.assertEqual(response.data['total_testcases'], 1)
        kafka_producer_cls.assert_not_called()

        submission = Submission.objects.get(id=response.data['id'])
        self.assertEqual(submission.status, 'SUCCESS')
        self.assertEqual(submission.output.strip(), 'practice ready')
        self.assertTrue(
            ExamEvent.objects.filter(submission=submission, event_type='submission.received').exists()
        )
        self.assertTrue(
            ExamEvent.objects.filter(submission=submission, event_type='submission.updated').exists()
        )
        self.assertFalse(
            ExamEvent.objects.filter(submission=submission, event_type='submission.queued').exists()
        )

    @patch('core_api.views.KafkaProducer')
    def test_practice_run_rejects_entry_file_language_mismatch(self, kafka_producer_cls):
        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print("hello")',
                'files': [
                    {'path': 'main.cpp', 'content': '#include <iostream>\nint main() { std::cout << 42 << "\\n"; return 0; }'},
                ],
                'entry_file': 'main.cpp',
                'language': 'python',
                'room_id': self.room.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('language', response.data)
        kafka_producer_cls.assert_not_called()

    @patch('core_api.views.get_missing_runtime_tools', return_value=['sqlite3'])
    @patch('core_api.views.KafkaProducer')
    def test_practice_run_falls_back_to_queue_when_inline_runtime_is_missing(self, kafka_producer_cls, _missing_runtime_tools):
        producer = kafka_producer_cls.return_value

        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'select 1;',
                'files': [
                    {'path': 'query.sql', 'content': 'select 1;'},
                ],
                'entry_file': 'query.sql',
                'language': 'sql',
                'room_id': self.room.id,
                'judge_cases': [
                    {
                        'input': '',
                        'expected_output': '1',
                    }
                ],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        producer.send.assert_called_once()
        topic, payload = producer.send.call_args[0]
        self.assertEqual(topic, get_submission_topic('sql'))
        self.assertEqual(payload['language'], 'sql')
        self.assertEqual(len(payload['judge_cases']), 1)

        submission = Submission.objects.get(id=response.data['id'])
        self.assertEqual(submission.status, 'PENDING')
        self.assertTrue(
            ExamEvent.objects.filter(submission=submission, event_type='submission.queued').exists()
        )


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-throttle-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [
            'core_api.throttles.DynamicQueueThrottle',
        ],
        'DEFAULT_THROTTLE_RATES': {
            'user': '1/minute',
            'burst': '1/second',
        },
    },
)
class SubmissionThrottleIntegrationTests(APITestCase):
    def setUp(self):
        core_views._kafka_producer = None
        self.teacher = User.objects.create_user(username='teacher-throttle', password='pass123')
        self.student = User.objects.create_user(username='student-throttle', password='pass123')
        self.token = Token.objects.create(user=self.student)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Throttle Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Throttle Question',
            description='Return the square.',
            testcase_input='2',
            expected_output='4',
            total_marks=10,
        )
        join_response = self.client.post(
            '/api/rooms/join/',
            {'room_code': self.room.room_code},
            format='json',
        )
        self.assertEqual(join_response.status_code, 201)

    def _prime_busy_queue(self, consumer, admin, topic_name='judge-vortex.submissions.python'):
        topic_partition = TopicPartition(topic_name, 0)
        consumer.partitions_for_topic.return_value = {0}
        consumer.end_offsets.return_value = {topic_partition: 5}
        admin.list_consumer_group_offsets.return_value = {topic_partition: SimpleNamespace(offset=0)}

    @patch('core_api.views.KafkaProducer')
    def test_queue_busy_uses_distributed_rate_limiter_block(self, kafka_producer_cls):
        with patch('core_api.throttles.KafkaManager.get_consumer') as get_consumer, patch(
            'core_api.throttles.KafkaManager.get_admin'
        ) as get_admin, patch(
            'core_api.throttles.get_topic_consumer_groups',
            return_value=[('judge-vortex.submissions.python', 'executor-core')],
        ), patch(
            'core_api.throttles.get_runtime_config',
            return_value={
                'distributed_rate_limiter': {
                    'enabled': True,
                    'mode': 'queue_busy',
                    'route': '/api/submissions/submit/',
                    'timeout_ms': 250,
                    'fail_open': True,
                },
                'queue_throttle': {
                    'allow_when_depth_at_or_below': 0,
                    'fallback_to_drf': True,
                },
            },
        ), patch(
            'core_api.throttles.evaluate_submission_rate_limit',
            return_value=ExternalRateLimitResult(
                allowed=False,
                applied=True,
                retry_after_seconds=9,
                policy_name='judge-vortex-submission-limit',
            ),
        ):
            consumer = get_consumer.return_value
            admin = get_admin.return_value
            self._prime_busy_queue(consumer, admin)

            response = self.client.post(
                '/api/submissions/submit/',
                {
                    'code': 'print(4)',
                    'language': 'python',
                    'room_id': self.room.id,
                    'question_id': self.question.id,
                },
                format='json',
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(Submission.objects.count(), 0)
        kafka_producer_cls.assert_not_called()

    @patch('core_api.views.KafkaProducer')
    def test_queue_busy_falls_back_to_local_drf_when_external_policy_is_missing(self, kafka_producer_cls):
        with patch('core_api.throttles.KafkaManager.get_consumer') as get_consumer, patch(
            'core_api.throttles.KafkaManager.get_admin'
        ) as get_admin, patch(
            'core_api.throttles.get_topic_consumer_groups',
            return_value=[('judge-vortex.submissions.python', 'executor-core')],
        ), patch(
            'core_api.throttles.get_runtime_config',
            return_value={
                'distributed_rate_limiter': {
                    'enabled': True,
                    'mode': 'queue_busy',
                    'route': '/api/submissions/submit/',
                    'timeout_ms': 250,
                    'fail_open': True,
                },
                'queue_throttle': {
                    'allow_when_depth_at_or_below': 0,
                    'fallback_to_drf': True,
                },
            },
        ), patch(
            'core_api.throttles.evaluate_submission_rate_limit',
            return_value=ExternalRateLimitResult(
                allowed=True,
                applied=False,
            ),
        ), patch.dict(
            'core_api.throttles.DynamicQueueThrottle.THROTTLE_RATES',
            {'user': '1/minute', 'burst': '1/second'},
        ):
            consumer = get_consumer.return_value
            admin = get_admin.return_value
            self._prime_busy_queue(consumer, admin)

            first = self.client.post(
                '/api/submissions/submit/',
                {
                    'code': 'print(4)',
                    'language': 'python',
                    'room_id': self.room.id,
                    'question_id': self.question.id,
                },
                format='json',
            )
            second = self.client.post(
                '/api/submissions/submit/',
                {
                    'code': 'print(4)',
                    'language': 'python',
                    'room_id': self.room.id,
                    'question_id': self.question.id,
                },
                format='json',
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(kafka_producer_cls.return_value.send.call_count, 1)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class TeacherRoomSubmissionFeedTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher-feed', password='pass123')
        self.student = User.objects.create_user(username='student-feed', password='pass123')
        self.teacher_token = Token.objects.create(user=self.teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Leaderboard Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Unique Score',
            description='Return the answer.',
            testcase_input='',
            expected_output='42',
            total_marks=10,
        )
        Submission.objects.create(
            user=self.student,
            room=self.room,
            question=self.question,
            code='print(42)',
            language='python',
            status='PASSED',
            awarded_marks=10,
        )

    def test_room_submission_feed_includes_student_and_question_ids(self):
        response = self.client.get(f'/api/rooms/{self.room.id}/submissions/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        submission_row = response.data[0]
        self.assertEqual(submission_row['student_id'], self.student.id)
        self.assertEqual(submission_row['question_id'], self.question.id)

    def test_room_submission_feed_keeps_code_snapshots_for_multiple_questions(self):
        question_two = ExamQuestion.objects.create(
            room=self.room,
            title='Second Question',
            description='Return another answer.',
            testcase_input='',
            expected_output='7',
            total_marks=10,
        )
        Submission.objects.create(
            user=self.student,
            room=self.room,
            question=question_two,
            code='print(7)',
            files=[{'path': 'main.py', 'content': 'print(7)'}],
            entry_file='main.py',
            language='python',
            status='WRONG_ANSWER',
        )

        response = self.client.get(f'/api/rooms/{self.room.id}/submissions/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row['question_id'] for row in response.data},
            {self.question.id, question_two.id},
        )
        self.assertTrue(any(row['code'] == 'print(42)' for row in response.data))
        self.assertTrue(any(row['code'] == 'print(7)' for row in response.data))


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class TeacherRoomParticipantPresenceTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher-presence', password='pass123')
        self.active_student = User.objects.create_user(username='student-active', password='pass123')
        self.stale_student = User.objects.create_user(username='student-stale', password='pass123')
        self.blocked_student = User.objects.create_user(username='student-blocked', password='pass123')
        self.teacher_token = Token.objects.create(user=self.teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Presence Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Assigned Snapshot Question',
            description='Return 1.',
            testcase_input='1',
            expected_output='1',
            total_marks=5,
        )

    def test_participant_feed_only_lists_active_or_blocked_students(self):
        active_participant = RoomParticipant.objects.create(
            room=self.room,
            student=self.active_student,
            last_presence_at=timezone.now(),
        )
        active_participant.assigned_questions.set([self.question])
        RoomParticipant.objects.create(
            room=self.room,
            student=self.stale_student,
            last_presence_at=timezone.now() - timedelta(minutes=10),
        )
        RoomParticipant.objects.create(
            room=self.room,
            student=self.blocked_student,
            access_locked=True,
            access_locked_at=timezone.now(),
        )

        response = self.client.get(f'/api/rooms/{self.room.id}/participants/')

        self.assertEqual(response.status_code, 200)
        rows_by_username = {row['username']: row for row in response.data}
        self.assertEqual(set(rows_by_username.keys()), {'student-active', 'student-blocked'})
        self.assertTrue(rows_by_username['student-active']['is_active'])
        self.assertFalse(rows_by_username['student-blocked']['is_active'])
        self.assertEqual(rows_by_username['student-active']['assigned_questions'][0]['id'], self.question.id)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class ExamWorkspaceSnapshotTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='teacher-snapshot', password='pass123')
        self.student = User.objects.create_user(username='student-snapshot', password='pass123')
        self.teacher_token = Token.objects.create(user=self.teacher)
        self.student_token = Token.objects.create(user=self.student)
        self.room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Snapshot Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=7),
        )
        self.question = ExamQuestion.objects.create(
            room=self.room,
            title='Live Draft',
            description='Return 5.',
            testcase_input='5',
            expected_output='5',
            total_marks=5,
        )
        self.participant = RoomParticipant.objects.create(room=self.room, student=self.student)
        self.participant.assigned_questions.set([self.question])

    def test_student_can_sync_workspace_snapshot(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.student_token.key}')

        response = self.client.post(
            f'/api/rooms/{self.room.id}/questions/{self.question.id}/snapshot/',
            {
                'language': 'python',
                'code': 'from helper import value\nprint(value())',
                'files': [
                    {'path': 'main.py', 'content': 'from helper import value\nprint(value())'},
                    {'path': 'helper.py', 'content': 'def value():\n    return 5'},
                ],
                'entry_file': 'main.py',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        snapshot = ExamWorkspaceSnapshot.objects.get(room=self.room, student=self.student, question=self.question)
        self.assertEqual(snapshot.entry_file, 'main.py')
        self.assertEqual(snapshot.language, 'python')
        self.assertEqual(len(snapshot.files), 2)
        self.participant.refresh_from_db()
        self.assertIsNotNone(self.participant.last_presence_at)

    def test_teacher_can_fetch_workspace_snapshots(self):
        ExamWorkspaceSnapshot.objects.create(
            room=self.room,
            student=self.student,
            question=self.question,
            language='python',
            code='print(5)',
            files=[{'path': 'main.py', 'content': 'print(5)'}],
            entry_file='main.py',
        )
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')

        response = self.client.get(f'/api/rooms/{self.room.id}/workspaces/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['student_id'], self.student.id)
        self.assertEqual(response.data[0]['question_id'], self.question.id)
        self.assertEqual(response.data[0]['source'], 'snapshot')


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class PlatformHealthTests(APITestCase):
    def test_liveness_endpoint_returns_ok(self):
        response = self.client.get('/api/health/live/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertEqual(response.data['service'], 'judge-vortex-web')

    @patch('core_api.views.check_tcp_dependency', return_value={'status': 'ok', 'host': '127.0.0.1', 'port': 9092})
    def test_readiness_endpoint_reports_dependencies(self, _kafka_check):
        response = self.client.get('/api/health/ready/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertEqual(response.data['checks']['database']['status'], 'ok')
        self.assertEqual(response.data['checks']['cache']['status'], 'ok')
        self.assertEqual(response.data['checks']['kafka']['status'], 'ok')
        self.assertIn('topics', response.data['checks']['submission_topics'])

    @patch('core_api.views.check_tcp_dependency', side_effect=RuntimeError('kafka down'))
    def test_readiness_endpoint_returns_503_when_dependency_is_down(self, _kafka_check):
        response = self.client.get('/api/health/ready/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['status'], 'degraded')
        self.assertEqual(response.data['checks']['kafka']['status'], 'error')


class SubmissionWorkspaceNormalizationTests(APITestCase):
    def test_normalize_submission_files_creates_default_entry_file(self):
        files, entry_file, code = core_views.normalize_submission_files('python', 'print(42)', None)

        self.assertEqual(entry_file, 'main.py')
        self.assertEqual(code, 'print(42)')
        self.assertEqual(files[0]['path'], 'main.py')

    def test_normalize_submission_files_respects_explicit_entry_file(self):
        files, entry_file, code = core_views.normalize_submission_files(
            'python',
            'print(42)',
            [
                {'path': 'src/hello.py', 'content': 'def hi():\n    return 42'},
                {'path': 'src/main.py', 'content': 'from hello import hi\nprint(hi())'},
            ],
            'src/main.py',
        )

        self.assertEqual(entry_file, 'src/main.py')
        self.assertEqual(code, 'from hello import hi\nprint(hi())')
        self.assertEqual(len(files), 2)

    def test_normalize_submission_files_rejects_unsafe_paths(self):
        with self.assertRaisesMessage(serializers.ValidationError, 'safe relative path'):
            core_views.normalize_submission_files(
                'python',
                'print(42)',
                [{'path': '../secrets.py', 'content': 'nope'}],
            )

    def test_validate_submission_language_accepts_matching_entry_file(self):
        language = core_views.validate_submission_language('cpp', 'src/main.cpp')

        self.assertEqual(language, 'cpp')

    def test_validate_submission_language_rejects_mismatched_entry_file(self):
        with self.assertRaisesMessage(serializers.ValidationError, 'must match the entry file'):
            core_views.validate_submission_language('python', 'src/main.cpp')

    def test_python_workspace_files_can_import_each_other(self):
        result = async_to_sync(core_views.run_code_in_sandbox)(
            'from hello import greet\nprint(greet())',
            'python',
            '',
            3000,
            files=[
                {'path': 'main.py', 'content': 'from hello import greet\nprint(greet())'},
                {'path': 'hello.py', 'content': 'def greet():\n    return "hello from helper"'},
            ],
            entry_file='main.py',
        )

        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['output'].strip(), 'hello from helper')

    def test_cpp_workspace_uses_entry_file_with_helper_sources(self):
        result = async_to_sync(core_views.run_code_in_sandbox)(
            '#include <iostream>\n#include "helper.h"\n\nint main() {\n    std::cout << greet();\n}\n',
            'cpp',
            '',
            3000,
            files=[
                {
                    'path': 'src/main.cpp',
                    'content': '#include <iostream>\n#include "helper.h"\n\nint main() {\n    std::cout << greet();\n}\n',
                },
                {
                    'path': 'src/helper.cpp',
                    'content': '#include "helper.h"\n\nconst char* greet() {\n    return "Fsdf";\n}\n',
                },
                {
                    'path': 'src/helper.h',
                    'content': 'const char* greet();\n',
                },
            ],
            entry_file='src/main.cpp',
        )

        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['output'].strip(), 'Fsdf')

    def test_build_testcases_splits_blank_line_delimited_cases(self):
        testcases = build_testcases('1\n\n2', '1\n\n4')

        self.assertEqual(len(testcases), 2)
        self.assertEqual(testcases[0]['input'], '1')
        self.assertEqual(testcases[1]['expected_output'], '4')


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'judge-vortex-tests',
        }
    },
    REST_FRAMEWORK={
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.TokenAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
        'DEFAULT_THROTTLE_CLASSES': [],
    },
)
class AuditTrailTests(APITestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(username='audit-teacher', password='pass123')
        self.student = User.objects.create_user(username='audit-student', password='pass123')
        self.teacher_token = Token.objects.create(user=self.teacher)
        self.student_token = Token.objects.create(user=self.student)

    def test_room_creation_logs_audit_event(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')
        response = self.client.post(
            '/api/rooms/',
            {
                'title': 'Audit Room',
                'questions_to_assign': 1,
                'join_deadline': (timezone.now() + timedelta(days=1)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        room = ExamRoom.objects.get(id=response.data['id'])
        event = ExamEvent.objects.get(room=room, event_type='room.created')
        self.assertEqual(event.actor, self.teacher)

    def test_teacher_can_fetch_room_event_timeline(self):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Timeline Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        ExamEvent.objects.create(
            room=room,
            actor=self.teacher,
            participant=self.student,
            event_type='participant.joined',
            message='student joined',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')
        response = self.client.get(f'/api/rooms/{room.id}/events/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['event_type'], 'participant.joined')

    def test_student_cannot_fetch_teacher_room_event_timeline(self):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Private Timeline Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.student_token.key}')
        response = self.client.get(f'/api/rooms/{room.id}/events/')

        self.assertEqual(response.status_code, 404)

    def test_teacher_unlock_logs_audit_event(self):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Unlock Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        participant = RoomParticipant.objects.create(room=room, student=self.student, access_locked=True)

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.teacher_token.key}')
        response = self.client.patch(
            f'/api/rooms/{room.id}/participants/{self.student.id}/',
            {'access_locked': False},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        participant.refresh_from_db()
        self.assertFalse(participant.access_locked)
        self.assertTrue(
            ExamEvent.objects.filter(room=room, participant=self.student, event_type='participant.unlocked').exists()
        )

    @patch('core_api.views.KafkaProducer')
    def test_submission_creation_logs_received_and_queued_events(self, kafka_producer_cls):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Submit Audit Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        question = ExamQuestion.objects.create(
            room=room,
            title='Audit Submit',
            description='Return 42.',
            testcase_input='42',
            expected_output='42',
            total_marks=10,
        )
        RoomParticipant.objects.create(room=room, student=self.student)
        RoomParticipant.objects.get(room=room, student=self.student).assigned_questions.set([question])

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.student_token.key}')
        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print(42)',
                'language': 'python',
                'room_id': room.id,
                'question_id': question.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        submission = Submission.objects.get(id=response.data['id'])
        self.assertTrue(ExamEvent.objects.filter(submission=submission, event_type='submission.received').exists())
        self.assertTrue(ExamEvent.objects.filter(submission=submission, event_type='submission.queued').exists())

    @patch('core_api.views.KafkaProducer')
    def test_submission_burst_creates_suspicious_event(self, kafka_producer_cls):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Burst Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        question = ExamQuestion.objects.create(
            room=room,
            title='Burst Question',
            description='Return 1.',
            testcase_input='1',
            expected_output='1',
            total_marks=5,
        )
        participant = RoomParticipant.objects.create(room=room, student=self.student)
        participant.assigned_questions.set([question])

        for _ in range(4):
            Submission.objects.create(
                user=self.student,
                room=room,
                question=question,
                code='print(1)',
                language='python',
                files=[],
                entry_file='main.py',
            )

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.student_token.key}')
        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'print(1)',
                'language': 'python',
                'room_id': room.id,
                'question_id': question.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        submission = Submission.objects.get(id=response.data['id'])
        self.assertTrue(
            ExamEvent.objects.filter(
                submission=submission,
                event_type='suspicious.submission_burst',
            ).exists()
        )

    @patch('core_api.views.KafkaProducer')
    def test_cross_question_code_reuse_creates_suspicious_event(self, kafka_producer_cls):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Reuse Room',
            questions_to_assign=2,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        question_one = ExamQuestion.objects.create(
            room=room,
            title='Question One',
            description='Return 1.',
            testcase_input='1',
            expected_output='1',
            total_marks=5,
        )
        question_two = ExamQuestion.objects.create(
            room=room,
            title='Question Two',
            description='Return 2.',
            testcase_input='2',
            expected_output='2',
            total_marks=5,
        )
        participant = RoomParticipant.objects.create(room=room, student=self.student)
        participant.assigned_questions.set([question_one, question_two])

        prior_files = [{'path': 'main.py', 'content': 'from helper import get\nprint(get())'}, {'path': 'helper.py', 'content': 'def get():\n    return 1'}]
        Submission.objects.create(
            user=self.student,
            room=room,
            question=question_one,
            code='from helper import get\nprint(get())',
            language='python',
            files=prior_files,
            entry_file='main.py',
        )

        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.student_token.key}')
        response = self.client.post(
            '/api/submissions/submit/',
            {
                'code': 'from helper import get\nprint(get())',
                'language': 'python',
                'room_id': room.id,
                'question_id': question_two.id,
                'files': prior_files,
                'entry_file': 'main.py',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        submission = Submission.objects.get(id=response.data['id'])
        self.assertTrue(
            ExamEvent.objects.filter(
                submission=submission,
                event_type='suspicious.cross_question_code_reuse',
            ).exists()
        )

    def test_failure_storm_creates_suspicious_event_on_update(self):
        room = ExamRoom.objects.create(
            teacher=self.teacher,
            title='Failure Storm Room',
            questions_to_assign=1,
            join_deadline=timezone.now() + timedelta(days=1),
        )
        question = ExamQuestion.objects.create(
            room=room,
            title='Storm Question',
            description='Return 1.',
            testcase_input='1',
            expected_output='1',
            total_marks=5,
        )

        for _ in range(3):
            Submission.objects.create(
                user=self.student,
                room=room,
                question=question,
                code='print(',
                language='python',
                status='COMPILATION_ERROR',
            )

        submission = Submission.objects.create(
            user=self.student,
            room=room,
            question=question,
            code='print(',
            language='python',
            status='PENDING',
        )

        response = self.client.patch(
            f'/api/submissions/{submission.id}/update/',
            {
                'status': 'COMPILATION_ERROR',
                'output': 'SyntaxError',
                'execution_time_ms': 0,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ExamEvent.objects.filter(
                submission=submission,
                event_type='suspicious.failure_storm',
            ).exists()
        )
