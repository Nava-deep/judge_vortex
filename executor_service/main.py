import json
import time
import asyncio
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
from grader import grade_submission
from prometheus_client import start_http_server, Counter

# --- CONFIGURATION ---
# Adjust this based on your CPU cores (e.g., 4 or 8)
MAX_CONCURRENT_EXECUTIONS = 5 

# Define our Prometheus metric
SUBMISSIONS_TOTAL = Counter('vortex_submissions_total', 'Total code executions')

# The Semaphore acts as our 'Worker Pool' bouncer
gatekeeper = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)

def get_kafka_consumer():
    """Attempts to connect to Kafka with a retry loop to handle startup lag."""
    while True:
        try:
            consumer = KafkaConsumer(
                'code_submissions',
                bootstrap_servers=['localhost:9092'],
                value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                group_id='judge_vortex_executor',
                api_version=(0, 10, 1) 
            )
            print("Successfully connected to Kafka!")
            return consumer
        except NoBrokersAvailable:
            print("Kafka brokers not available yet... retrying in 2 seconds")
            time.sleep(2)
        except Exception as e:
            print(f"Unexpected error connecting to Kafka: {e}")
            time.sleep(5)

async def run_grade_task(submission_data):
    """
    Acquires a slot from the semaphore, runs the grader in a thread, 
    and then releases the slot.
    """
    async with gatekeeper:
        SUBMISSIONS_TOTAL.inc()
        sub_id = submission_data.get('submission_id', 'Unknown')
        print(f"Processing ID: {sub_id} (Slot Acquired)")
        
        try:
            # We run the synchronous grade_submission in a thread pool 
            # so it doesn't block other tasks from starting.
            await asyncio.to_thread(grade_submission, submission_data)
            print(f"Finished processing ID: {sub_id} (Slot Released)")
        except Exception as e:pass

async def start_worker():
    # 1. Start the Prometheus metrics server
    start_http_server(8001)
    print("Metrics exported at http://localhost:8001/metrics")
    
    # 2. Get the consumer using our retry logic
    # Note: KafkaConsumer is synchronous, so we run it in a loop
    consumer = get_kafka_consumer()
    
    print(f"Executor Sandbox running (Concurrency Limit: {MAX_CONCURRENT_EXECUTIONS})")
    
    # 3. Main processing loop
    while True:
        # poll() allows us to check Kafka without blocking the whole script forever
        messages = consumer.poll(timeout_ms=1000)
        
        for topic_partition, msg_list in messages.items():
            for message in msg_list:
                submission_data = message.value
                print(f"\n[RECEIVED] Submission ID: {submission_data.get('submission_id')}")
                
                # Create a non-blocking task for this submission
                # This goes into the background and waits for a Semaphore slot
                asyncio.create_task(run_grade_task(submission_data))
        
        # Brief yield to the event loop
        await asyncio.sleep(0.01)

if __name__ == '__main__':
    try:
        asyncio.run(start_worker())
    except KeyboardInterrupt:
        print("\nExecutor shutting down...")