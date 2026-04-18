from prometheus_client import Counter, Gauge, Histogram


SUBMISSIONS_RECEIVED_TOTAL = Counter(
    'vortex_submissions_received_total',
    'Submissions accepted by the web tier.',
    labelnames=('language', 'mode'),
)
SUBMISSIONS_QUEUED_TOTAL = Counter(
    'vortex_submissions_queued_total',
    'Submissions published to Kafka.',
    labelnames=('language', 'topic'),
)
SUBMISSION_VERDICTS_TOTAL = Counter(
    'vortex_submission_verdicts_total',
    'Submission verdicts persisted by the web tier.',
    labelnames=('language', 'status'),
)
SUBMISSION_END_TO_END_SECONDS = Histogram(
    'vortex_submission_end_to_end_seconds',
    'Wall-clock time between submission creation and final verdict persistence.',
    labelnames=('language', 'status'),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
ROOM_JOINS_TOTAL = Counter(
    'vortex_room_joins_total',
    'Student join and resume attempts for exam rooms.',
    labelnames=('result',),
)
PARTICIPANT_LOCK_EVENTS_TOTAL = Counter(
    'vortex_participant_lock_events_total',
    'Participant moderation and lockout actions.',
    labelnames=('source', 'action'),
)
WEBSOCKET_DELIVERIES_TOTAL = Counter(
    'vortex_websocket_deliveries_total',
    'WebSocket lifecycle and delivery events.',
    labelnames=('event', 'result'),
)
QUEUE_DEPTH_GAUGE = Gauge(
    'vortex_submission_queue_depth',
    'Current estimated Kafka submission queue depth across executor topics.',
)
READINESS_CHECK_TOTAL = Counter(
    'vortex_readiness_checks_total',
    'Dependency readiness checks performed by the web tier.',
    labelnames=('dependency', 'result'),
)
AUDIT_EVENTS_TOTAL = Counter(
    'vortex_audit_events_total',
    'Exam audit events written to the database.',
    labelnames=('event_type', 'severity'),
)
SUSPICIOUS_EVENTS_TOTAL = Counter(
    'vortex_suspicious_events_total',
    'Suspicious execution pattern events detected by the web tier.',
    labelnames=('pattern',),
)
