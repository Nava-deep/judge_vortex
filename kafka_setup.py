from kafka.admin import KafkaAdminClient, NewTopic
import logging

# Set up logging so we can see what happens
logging.basicConfig(level=logging.INFO)

try:
    # Connect to your local Kafka
    admin_client = KafkaAdminClient(bootstrap_servers="localhost:9092", client_id='setup_script')

    # Define the new bucket/topic
    topic_list = [NewTopic(name="code_submissions", num_partitions=1, replication_factor=1)]
    
    # Create it
    admin_client.create_topics(new_topics=topic_list, validate_only=False)
    print("✅ SUCCESS: 'code_submissions' topic created perfectly!")

except Exception as e:
    if "TopicAlreadyExistsError" in str(e):
        print("✅ SUCCESS: The topic already exists, you are good to go!")
    else:
        print(f"❌ ERROR: {e}")