# core/throttles.py
from rest_framework.throttling import UserRateThrottle
from kafka import KafkaConsumer, TopicPartition
import logging

logger = logging.getLogger(__name__)

class KafkaManager:
    """Singleton to keep Kafka connection alive across requests"""
    _consumer = None

    @classmethod
    def get_consumer(cls):
        if cls._consumer is None:
            cls._consumer = KafkaConsumer(
                bootstrap_servers=['localhost:9092'],
                api_version=(0, 10, 1),
                # Reduced timeout so the web request doesn't hang if Kafka is slow
                request_timeout_ms=1000, 
                connections_max_idle_ms=30000
            )
        return cls._consumer

class DynamicQueueThrottle(UserRateThrottle):
    def allow_request(self, request, view):
        # 🟢 FIX: Only throttle POST requests (Submissions).
        # This ensures GET requests (fetching results) are NEVER blocked.
        if request.method != 'POST':
            return True

        if request.user and request.user.is_staff:
            return True

        try:
            consumer = KafkaManager.get_consumer()
            tp = TopicPartition('code_submissions', 0)
            
            # 1. Get the 'High Watermark' (Total messages in Kafka)
            end_offsets = consumer.end_offsets([tp])
            high_watermark = end_offsets.get(tp, 0)

            # 2. Get the current position of the 'judge_vortex_executor' group
            # We use position(tp) to see where the consumer is currently standing.
            current_position = consumer.position(tp)

            queue_depth = high_watermark - current_position

            # 🚀 PRODUCTION LOGIC:
            # If the queue is empty (depth <= 0), allow the request immediately.
            if queue_depth <= 0:
                return True

        except Exception as e:
            logger.error(f"Kafka Throttle Error: {e}")
            # Fallback to standard throttling if Kafka is unreachable
            pass

        # If queue is busy, enforce the 'user' limit defined in settings.py
        return super().allow_request(request, view)