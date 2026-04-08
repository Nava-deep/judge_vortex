import json
import logging

from .metrics import AUDIT_EVENTS_TOTAL
from .models import ExamEvent

logger = logging.getLogger(__name__)


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def sanitize_metadata(metadata):
    if not metadata:
        return {}
    return {str(key): _json_safe(value) for key, value in metadata.items()}


def record_exam_event(*, event_type, message, severity='info', room=None, question=None, submission=None, actor=None, participant=None, metadata=None):
    payload = sanitize_metadata(metadata)
    event = ExamEvent.objects.create(
        event_type=event_type,
        message=message,
        severity=severity,
        room=room,
        question=question,
        submission=submission,
        actor=actor,
        participant=participant,
        metadata=payload,
    )
    AUDIT_EVENTS_TOTAL.labels(event_type=event_type, severity=severity).inc()
    logger.info(
        'audit.event %s',
        json.dumps(
            {
                'event_type': event_type,
                'severity': severity,
                'room_id': room.id if room else None,
                'question_id': question.id if question else None,
                'submission_id': submission.id if submission else None,
                'actor_id': actor.id if actor else None,
                'participant_id': participant.id if participant else None,
                'metadata': payload,
            },
            sort_keys=True,
            default=str,
        ),
    )
    return event
