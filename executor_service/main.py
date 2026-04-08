import asyncio
import json
import os
import time
from collections import defaultdict
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
from kafka.structs import OffsetAndMetadata
from grader import grade_submission, update_submission
from prometheus_client import start_http_server
from observability import (
    EXECUTOR_FAILURES_TOTAL,
    EXECUTOR_INFLIGHT,
    EXECUTOR_PROCESSING_SECONDS,
    EXECUTOR_SUBMISSIONS_TOTAL,
    EXECUTOR_VERDICTS_TOTAL,
    log_executor_event,
)

# --- CONFIGURATION ---
KAFKA_SUBMISSIONS_TOPIC = os.getenv("KAFKA_SUBMISSIONS_TOPIC", "code_submissions")
KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092").split(",") if server.strip()]
KAFKA_EXECUTOR_GROUP = os.getenv("KAFKA_EXECUTOR_GROUP", "judge_vortex_executor")
EXECUTOR_PROMETHEUS_PORT = int(os.getenv("EXECUTOR_PROMETHEUS_PORT", "8001"))
MAX_CONCURRENT_EXECUTIONS = max(2, int(os.getenv("EXECUTOR_MAX_CONCURRENCY", min(os.cpu_count() or 4, 8))))
EXECUTOR_NAME = os.getenv("EXECUTOR_NAME", "executor")
SUPPORTED_LANGUAGES = {
    language.strip().lower()
    for language in os.getenv("EXECUTOR_SUPPORTED_LANGUAGES", "").split(",")
    if language.strip()
}

# The Semaphore acts as our 'Worker Pool' bouncer
gatekeeper = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)


class CommitTracker:
    """Commit Kafka offsets only after grading is durably finished."""

    def __init__(self, consumer):
        self.consumer = consumer
        self.next_commit_offsets = {}
        self.completed_offsets = defaultdict(set)
        self.lock = asyncio.Lock()

    def _get_next_commit_offset(self, topic_partition, message_offset):
        next_offset = self.next_commit_offsets.get(topic_partition)
        if next_offset is not None:
            return next_offset

        committed = self.consumer.committed(topic_partition)
        if committed is None or committed < 0:
            committed = message_offset

        self.next_commit_offsets[topic_partition] = committed
        return committed

    async def mark_completed(self, topic_partition, message_offset):
        async with self.lock:
            next_offset = self._get_next_commit_offset(topic_partition, message_offset)
            if message_offset < next_offset:
                return

            self.completed_offsets[topic_partition].add(message_offset)
            candidate_offset = next_offset
            while candidate_offset in self.completed_offsets[topic_partition]:
                candidate_offset += 1

            if candidate_offset == next_offset:
                return

            await asyncio.to_thread(
                self.consumer.commit,
                offsets={topic_partition: OffsetAndMetadata(candidate_offset, "", -1)},
            )

            for offset in range(next_offset, candidate_offset):
                self.completed_offsets[topic_partition].discard(offset)
            self.next_commit_offsets[topic_partition] = candidate_offset

def get_kafka_consumer():
    """Attempts to connect to Kafka with a retry loop to handle startup lag."""
    while True:
        try:
            consumer = KafkaConsumer(
                KAFKA_SUBMISSIONS_TOPIC,
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                group_id=KAFKA_EXECUTOR_GROUP,
                api_version=(0, 10, 1),
                auto_offset_reset="earliest",
                enable_auto_commit=False,
                max_poll_records=MAX_CONCURRENT_EXECUTIONS,
            )
            log_executor_event('executor.kafka.connected', executor=EXECUTOR_NAME, topic=KAFKA_SUBMISSIONS_TOPIC, bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
            return consumer
        except NoBrokersAvailable:
            log_executor_event('executor.kafka.waiting', executor=EXECUTOR_NAME, topic=KAFKA_SUBMISSIONS_TOPIC, retry_in_seconds=2)
            time.sleep(2)
        except Exception as e:
            log_executor_event('executor.kafka.error', executor=EXECUTOR_NAME, topic=KAFKA_SUBMISSIONS_TOPIC, error=str(e))
            time.sleep(5)

async def run_grade_task(submission_data, topic_partition, message_offset, commit_tracker):
    """
    Acquires a slot from the semaphore, grades the submission, and commits
    the Kafka offset only after the result has been persisted back to Django.
    """
    async with gatekeeper:
        sub_id = submission_data.get('submission_id', 'Unknown')
        language = str(submission_data.get('language', '')).strip().lower()
        EXECUTOR_SUBMISSIONS_TOTAL.labels(executor=EXECUTOR_NAME, language=language or 'unknown').inc()
        EXECUTOR_INFLIGHT.labels(executor=EXECUTOR_NAME).inc()
        started_at = time.perf_counter()
        log_executor_event(
            'executor.task.start',
            executor=EXECUTOR_NAME,
            submission_id=sub_id,
            topic=topic_partition.topic,
            partition=topic_partition.partition,
            offset=message_offset,
            language=language,
        )
        
        try:
            if SUPPORTED_LANGUAGES and language not in SUPPORTED_LANGUAGES:
                await update_submission(
                    sub_id,
                    "SYSTEM_ERROR",
                    f"Executor route mismatch in {EXECUTOR_NAME}: unsupported language '{language}'.",
                    0,
                )
                EXECUTOR_VERDICTS_TOTAL.labels(executor=EXECUTOR_NAME, language=language or 'unknown', status='SYSTEM_ERROR').inc()
                await commit_tracker.mark_completed(topic_partition, message_offset)
                log_executor_event('executor.task.rejected', executor=EXECUTOR_NAME, submission_id=sub_id, language=language, reason='unsupported_language_route')
                return

            await grade_submission(submission_data)
            await commit_tracker.mark_completed(topic_partition, message_offset)
            log_executor_event('executor.task.finished', executor=EXECUTOR_NAME, submission_id=sub_id, language=language)
        except Exception as e:
            EXECUTOR_FAILURES_TOTAL.labels(executor=EXECUTOR_NAME, stage='grade').inc()
            log_executor_event('executor.task.error', executor=EXECUTOR_NAME, submission_id=sub_id, language=language, error=str(e))
            try:
                await update_submission(sub_id, "SYSTEM_ERROR", f"Executor error: {e}", 0)
                EXECUTOR_VERDICTS_TOTAL.labels(executor=EXECUTOR_NAME, language=language or 'unknown', status='SYSTEM_ERROR').inc()
                await commit_tracker.mark_completed(topic_partition, message_offset)
            except Exception as update_error:
                EXECUTOR_FAILURES_TOTAL.labels(executor=EXECUTOR_NAME, stage='callback').inc()
                log_executor_event('executor.task.callback_failed', executor=EXECUTOR_NAME, submission_id=sub_id, language=language, error=str(update_error))
        finally:
            EXECUTOR_INFLIGHT.labels(executor=EXECUTOR_NAME).dec()
            EXECUTOR_PROCESSING_SECONDS.labels(executor=EXECUTOR_NAME, language=language or 'unknown').observe(max(time.perf_counter() - started_at, 0.0))

async def start_worker():
    # 1. Start the Prometheus metrics server
    start_http_server(EXECUTOR_PROMETHEUS_PORT)
    log_executor_event('executor.metrics.ready', executor=EXECUTOR_NAME, port=EXECUTOR_PROMETHEUS_PORT)
    
    # 2. Get the consumer using our retry logic
    # Note: KafkaConsumer is synchronous, so we run it in a loop
    consumer = get_kafka_consumer()
    commit_tracker = CommitTracker(consumer)
    active_tasks = set()
    is_paused = False
    
    log_executor_event(
        'executor.ready',
        executor=EXECUTOR_NAME,
        topic=KAFKA_SUBMISSIONS_TOPIC,
        concurrency=MAX_CONCURRENT_EXECUTIONS,
        supported_languages=sorted(SUPPORTED_LANGUAGES),
    )
    
    # 3. Main processing loop
    while True:
        assignment = consumer.assignment()
        if len(active_tasks) >= MAX_CONCURRENT_EXECUTIONS and assignment and not is_paused:
            consumer.pause(*assignment)
            is_paused = True
        elif len(active_tasks) < MAX_CONCURRENT_EXECUTIONS and is_paused:
            paused_partitions = consumer.paused()
            if paused_partitions:
                consumer.resume(*paused_partitions)
            is_paused = False

        # poll() allows us to check Kafka without blocking the whole script forever
        messages = consumer.poll(timeout_ms=100)
        
        for topic_partition, msg_list in messages.items():
            for message in msg_list:
                submission_data = message.value
                log_executor_event(
                    'executor.message.received',
                    executor=EXECUTOR_NAME,
                    submission_id=submission_data.get('submission_id'),
                    topic=topic_partition.topic,
                    partition=topic_partition.partition,
                    offset=message.offset,
                )
                
                # Create a non-blocking task for this submission
                # This goes into the background and waits for a Semaphore slot
                task = asyncio.create_task(
                    run_grade_task(
                        submission_data,
                        topic_partition,
                        message.offset,
                        commit_tracker,
                    )
                )
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
        
        # Brief yield to the event loop
        await asyncio.sleep(0.01)

if __name__ == '__main__':
    try:
        asyncio.run(start_worker())
    except KeyboardInterrupt:
        log_executor_event('executor.shutdown', executor=EXECUTOR_NAME)
