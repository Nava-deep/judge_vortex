import asyncio
import json
import os
import time
from collections import defaultdict
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
from kafka.structs import OffsetAndMetadata
from grader import grade_submission, update_submission
from prometheus_client import start_http_server, Counter

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

# Define our Prometheus metric
SUBMISSIONS_TOTAL = Counter('vortex_submissions_total', 'Total code executions')

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
            print("Successfully connected to Kafka!")
            return consumer
        except NoBrokersAvailable:
            print("Kafka brokers not available yet... retrying in 2 seconds")
            time.sleep(2)
        except Exception as e:
            print(f"Unexpected error connecting to Kafka: {e}")
            time.sleep(5)

async def run_grade_task(submission_data, topic_partition, message_offset, commit_tracker):
    """
    Acquires a slot from the semaphore, grades the submission, and commits
    the Kafka offset only after the result has been persisted back to Django.
    """
    async with gatekeeper:
        SUBMISSIONS_TOTAL.inc()
        sub_id = submission_data.get('submission_id', 'Unknown')
        language = str(submission_data.get('language', '')).strip().lower()
        print(f"Processing ID: {sub_id} (Slot Acquired)")
        
        try:
            if SUPPORTED_LANGUAGES and language not in SUPPORTED_LANGUAGES:
                await update_submission(
                    sub_id,
                    "SYSTEM_ERROR",
                    f"Executor route mismatch in {EXECUTOR_NAME}: unsupported language '{language}'.",
                    0,
                )
                await commit_tracker.mark_completed(topic_partition, message_offset)
                print(f"Rejected ID: {sub_id} due to unsupported language route (Slot Released)")
                return

            await grade_submission(submission_data)
            await commit_tracker.mark_completed(topic_partition, message_offset)
            print(f"Finished processing ID: {sub_id} (Slot Released)")
        except Exception as e:
            print(f"Executor error while processing {sub_id}: {e}")
            try:
                await update_submission(sub_id, "SYSTEM_ERROR", f"Executor error: {e}", 0)
                await commit_tracker.mark_completed(topic_partition, message_offset)
            except Exception as update_error:
                print(f"Failed to persist executor error for {sub_id}: {update_error}")

async def start_worker():
    # 1. Start the Prometheus metrics server
    start_http_server(EXECUTOR_PROMETHEUS_PORT)
    print(f"Metrics exported at http://localhost:{EXECUTOR_PROMETHEUS_PORT}/metrics")
    
    # 2. Get the consumer using our retry logic
    # Note: KafkaConsumer is synchronous, so we run it in a loop
    consumer = get_kafka_consumer()
    commit_tracker = CommitTracker(consumer)
    active_tasks = set()
    is_paused = False
    
    print(
        f"{EXECUTOR_NAME} running for topic '{KAFKA_SUBMISSIONS_TOPIC}' "
        f"(Concurrency Limit: {MAX_CONCURRENT_EXECUTIONS})"
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
                print(f"\n[RECEIVED] Submission ID: {submission_data.get('submission_id')}")
                
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
        print("\nExecutor shutting down...")
