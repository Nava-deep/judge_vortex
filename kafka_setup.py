import logging
import os
import sys
import time
from kafka.admin import KafkaAdminClient, NewPartitions, NewTopic
from execution_routing import get_all_submission_topics

logging.basicConfig(level=logging.INFO)

KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092").split(",") if server.strip()]
KAFKA_TOPIC_PARTITIONS = max(1, int(os.getenv("KAFKA_SUBMISSIONS_TOPIC_PARTITIONS", "8")))
KAFKA_SETUP_MAX_RETRIES = max(1, int(os.getenv("KAFKA_SETUP_MAX_RETRIES", "30")))
KAFKA_SETUP_BASE_DELAY_SEC = max(0.2, float(os.getenv("KAFKA_SETUP_BASE_DELAY_SEC", "1.0")))


def get_admin_client():
    last_error = None
    for attempt in range(1, KAFKA_SETUP_MAX_RETRIES + 1):
        try:
            return KafkaAdminClient(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                client_id="setup_script",
            )
        except Exception as exc:
            last_error = exc
            if attempt == KAFKA_SETUP_MAX_RETRIES:
                break
            wait_seconds = min(KAFKA_SETUP_BASE_DELAY_SEC * attempt, 5.0)
            print(
                f"WAIT: Kafka is not ready yet ({exc}). "
                f"Retrying in {wait_seconds:.1f}s [{attempt}/{KAFKA_SETUP_MAX_RETRIES}]"
            )
            time.sleep(wait_seconds)

    raise last_error


admin_client = None
try:
    admin_client = get_admin_client()

    submission_topics = list(get_all_submission_topics())
    described_topics = {
        topic["topic"]: topic
        for topic in admin_client.describe_topics(submission_topics)
    }

    for topic_name in submission_topics:
        topic_info = described_topics.get(topic_name)

        if topic_info is None or topic_info.get("error_code") != 0:
            admin_client.create_topics(
                new_topics=[
                    NewTopic(
                        name=topic_name,
                        num_partitions=KAFKA_TOPIC_PARTITIONS,
                        replication_factor=1,
                    )
                ],
                validate_only=False,
            )
            print(f"SUCCESS: '{topic_name}' topic created with {KAFKA_TOPIC_PARTITIONS} partitions.")
            continue

        current_partitions = len(topic_info.get("partitions", []))
        if current_partitions < KAFKA_TOPIC_PARTITIONS:
            admin_client.create_partitions({
                topic_name: NewPartitions(total_count=KAFKA_TOPIC_PARTITIONS)
            })
            print(
                f"SUCCESS: '{topic_name}' partitions increased "
                f"from {current_partitions} to {KAFKA_TOPIC_PARTITIONS}."
            )
        else:
            print(f"SUCCESS: '{topic_name}' already exists with {current_partitions} partitions.")

except Exception as exc:
    if "TopicAlreadyExistsError" in str(exc):
        print("SUCCESS: The topic already exists, you are good to go!")
    else:
        print(f"ERROR: {exc}")
        sys.exit(1)
finally:
    if admin_client is not None:
        admin_client.close()
