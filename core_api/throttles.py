import logging
import os
from rest_framework.throttling import UserRateThrottle
from kafka import KafkaConsumer, TopicPartition
from kafka.admin import KafkaAdminClient
from execution_routing import get_topic_consumer_groups

logger = logging.getLogger(__name__)
KAFKA_BOOTSTRAP_SERVERS = [server.strip() for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092").split(",") if server.strip()]

class KafkaManager:
    """Singleton to keep Kafka connection alive across requests"""
    _consumer = None
    _admin = None

    @classmethod
    def get_consumer(cls):
        if cls._consumer is None:
            cls._consumer = KafkaConsumer(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                api_version=(0, 10, 1),
                # Reduced timeout so the web request doesn't hang if Kafka is slow
                request_timeout_ms=1000, 
                connections_max_idle_ms=30000
            )
        return cls._consumer

    @classmethod
    def get_admin(cls):
        if cls._admin is None:
            cls._admin = KafkaAdminClient(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                client_id="judge_vortex_throttle",
            )
        return cls._admin

class DynamicQueueThrottle(UserRateThrottle):
    def allow_request(self, request, view):
        # Only throttle the submission endpoint.
        # Other POST actions such as room/question management should never be
        # blocked by queue depth or Redis throttling.
        if request.method != 'POST' or not request.path.rstrip('/').endswith('/api/submissions/submit'):
            return True

        if request.user and request.user.is_staff:
            return True

        try:
            consumer = KafkaManager.get_consumer()
            admin = KafkaManager.get_admin()

            queue_depth = 0
            for topic_name, consumer_group in get_topic_consumer_groups():
                partitions = consumer.partitions_for_topic(topic_name) or set()
                if not partitions:
                    continue

                topic_partitions = [TopicPartition(topic_name, partition) for partition in sorted(partitions)]
                end_offsets = consumer.end_offsets(topic_partitions)
                committed_offsets = admin.list_consumer_group_offsets(
                    consumer_group,
                    partitions=topic_partitions,
                )

                for tp in topic_partitions:
                    high_watermark = end_offsets.get(tp, 0) or 0
                    committed_meta = committed_offsets.get(tp)
                    committed = getattr(committed_meta, "offset", None)
                    if committed is None or committed < 0:
                        committed = 0
                    queue_depth += max(high_watermark - committed, 0)

            # 🚀 PRODUCTION LOGIC:
            # If the queue is empty (depth <= 0), allow the request immediately.
            if queue_depth <= 0:
                return True

        except Exception as e:
            logger.error(f"Kafka Throttle Error: {e}")
            # Fall through to the standard DRF throttle below only if the cache
            # backend is healthy. If Redis is also unavailable, fail open.

        # If queue is busy, enforce the 'user' limit defined in settings.py
        try:
            return super().allow_request(request, view)
        except Exception as e:
            logger.error(f"Throttle Cache Error: {e}")
            return True
