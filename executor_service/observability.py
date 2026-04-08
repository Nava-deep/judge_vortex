import json
import logging

from prometheus_client import Counter, Gauge, Histogram


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger('judge_vortex.executor')

EXECUTOR_SUBMISSIONS_TOTAL = Counter(
    'vortex_executor_submissions_total',
    'Submissions consumed by executor workers.',
    labelnames=('executor', 'language'),
)
EXECUTOR_VERDICTS_TOTAL = Counter(
    'vortex_executor_verdicts_total',
    'Verdicts produced by executor workers.',
    labelnames=('executor', 'language', 'status'),
)
EXECUTOR_FAILURES_TOTAL = Counter(
    'vortex_executor_failures_total',
    'Executor failures grouped by stage.',
    labelnames=('executor', 'stage'),
)
EXECUTOR_INFLIGHT = Gauge(
    'vortex_executor_inflight',
    'Currently running grading tasks.',
    labelnames=('executor',),
)
EXECUTOR_PROCESSING_SECONDS = Histogram(
    'vortex_executor_processing_seconds',
    'Submission processing time inside an executor worker.',
    labelnames=('executor', 'language'),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
EXECUTOR_CALLBACK_TOTAL = Counter(
    'vortex_executor_callback_total',
    'Executor callback attempts back to Django.',
    labelnames=('executor', 'result'),
)


def log_executor_event(event_type, **payload):
    logger.info('%s %s', event_type, json.dumps(payload, sort_keys=True, default=str))
