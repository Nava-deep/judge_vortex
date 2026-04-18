import hashlib
import json
import os
from datetime import timedelta

from django.utils import timezone

from .audit import record_exam_event
from .metrics import SUSPICIOUS_EVENTS_TOTAL
from .models import ExamEvent, Submission


SUBMISSION_BURST_WINDOW_SECONDS = max(10, int(os.getenv("SUSPICIOUS_SUBMISSION_BURST_WINDOW_SECONDS", "45")))
SUBMISSION_BURST_THRESHOLD = max(2, int(os.getenv("SUSPICIOUS_SUBMISSION_BURST_THRESHOLD", "5")))
FAILURE_STORM_WINDOW_SECONDS = max(30, int(os.getenv("SUSPICIOUS_FAILURE_STORM_WINDOW_SECONDS", "180")))
FAILURE_STORM_THRESHOLD = max(2, int(os.getenv("SUSPICIOUS_FAILURE_STORM_THRESHOLD", "4")))
PATTERN_DEDUP_WINDOW_SECONDS = max(30, int(os.getenv("SUSPICIOUS_EVENT_DEDUP_WINDOW_SECONDS", "600")))
FAILURE_STATUSES = {"COMPILATION_ERROR", "RUNTIME_ERROR", "SYSTEM_ERROR"}


def _normalize_workspace_signature(submission):
    files = submission.files or []
    normalized_files = sorted(
        (
            {
                "path": str(file_info.get("path", "")).strip(),
                "content": file_info.get("content", ""),
            }
            for file_info in files
            if isinstance(file_info, dict)
        ),
        key=lambda item: (item["path"], item["content"]),
    )
    payload = {
        "entry_file": submission.entry_file or "",
        "language": submission.language,
        "code": submission.code or "",
        "files": normalized_files,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pattern_logged_recently(submission, event_type):
    if not submission.room_id or not submission.user_id:
        return False
    since = timezone.now() - timedelta(seconds=PATTERN_DEDUP_WINDOW_SECONDS)
    return ExamEvent.objects.filter(
        room=submission.room,
        participant=submission.user,
        event_type=event_type,
        created_at__gte=since,
    ).exists()


def _record_suspicious_event(submission, event_type, message, metadata):
    if _pattern_logged_recently(submission, event_type):
        return None
    pattern = event_type.split(".", 1)[-1]
    SUSPICIOUS_EVENTS_TOTAL.labels(pattern=pattern).inc()
    return record_exam_event(
        event_type=event_type,
        message=message,
        severity="warning",
        room=submission.room,
        question=submission.question,
        submission=submission,
        actor=submission.user,
        participant=submission.user,
        metadata=metadata,
    )


def detect_suspicious_submission_patterns(submission):
    events = []
    if not submission.room_id or not submission.question_id:
        return events

    burst_since = timezone.now() - timedelta(seconds=SUBMISSION_BURST_WINDOW_SECONDS)
    recent_count = Submission.objects.filter(
        user=submission.user,
        room=submission.room,
        submitted_at__gte=burst_since,
    ).count()
    if recent_count >= SUBMISSION_BURST_THRESHOLD:
        event = _record_suspicious_event(
            submission,
            "suspicious.submission_burst",
            f"{submission.user.username} submitted {recent_count} times in under {SUBMISSION_BURST_WINDOW_SECONDS} seconds.",
            {
                "recent_submission_count": recent_count,
                "window_seconds": SUBMISSION_BURST_WINDOW_SECONDS,
            },
        )
        if event:
            events.append(event)

    workspace_signature = _normalize_workspace_signature(submission)
    reused_submission = Submission.objects.filter(
        user=submission.user,
        room=submission.room,
        language=submission.language,
    ).exclude(
        id=submission.id,
    ).exclude(
        question_id=submission.question_id,
    ).order_by("-submitted_at")

    for prior_submission in reused_submission:
        if _normalize_workspace_signature(prior_submission) != workspace_signature:
            continue
        event = _record_suspicious_event(
            submission,
            "suspicious.cross_question_code_reuse",
            f"{submission.user.username} reused an identical workspace across multiple exam questions.",
            {
                "current_question_id": submission.question_id,
                "prior_question_id": prior_submission.question_id,
                "prior_submission_id": prior_submission.id,
                "workspace_signature": workspace_signature,
            },
        )
        if event:
            events.append(event)
        break

    return events


def detect_suspicious_result_patterns(submission):
    events = []
    if not submission.room_id or submission.status not in FAILURE_STATUSES:
        return events

    failure_since = timezone.now() - timedelta(seconds=FAILURE_STORM_WINDOW_SECONDS)
    recent_failures = Submission.objects.filter(
        user=submission.user,
        room=submission.room,
        status__in=FAILURE_STATUSES,
        submitted_at__gte=failure_since,
    ).count()
    if recent_failures >= FAILURE_STORM_THRESHOLD:
        event = _record_suspicious_event(
            submission,
            "suspicious.failure_storm",
            f"{submission.user.username} triggered {recent_failures} execution failures in a short window.",
            {
                "recent_failure_count": recent_failures,
                "window_seconds": FAILURE_STORM_WINDOW_SECONDS,
                "latest_status": submission.status,
            },
        )
        if event:
            events.append(event)
    return events
