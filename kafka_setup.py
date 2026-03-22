import logging
import os
from kafka.admin import KafkaAdminClient, NewPartitions, NewTopic
from execution_routing import get_all_submission_topics

logging.basicConfig(level=logging.INFO)

KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092").split(",") if server.strip()]
KAFKA_TOPIC_PARTITIONS = max(1, int(os.getenv("KAFKA_SUBMISSIONS_TOPIC_PARTITIONS", "8")))

try:
    admin_client = KafkaAdminClient(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        client_id="setup_script",
    )

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
