from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from . import views as core_views
from .models import ExamQuestion, ExamRoom, RoomParticipant, Submission
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
        self.assertEqual(kick_response.status_code, 204)

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
