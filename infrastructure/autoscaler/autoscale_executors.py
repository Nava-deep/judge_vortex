#!/usr/bin/env python3

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from kafka import KafkaConsumer, TopicPartition
from kafka.admin import KafkaAdminClient

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from execution_routing import get_executor_routes  # noqa: E402


KAFKA_BOOTSTRAP_SERVERS = [
    server.strip()
    for server in os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092").split(",")
    if server.strip()
]
AUTOSCALER_INTERVAL_SECONDS = max(5, int(os.getenv("AUTOSCALER_INTERVAL_SECONDS", "15")))
AUTOSCALER_LAG_PER_REPLICA = max(1, int(os.getenv("AUTOSCALER_LAG_PER_REPLICA", "12")))
AUTOSCALER_COOLDOWN_SECONDS = max(0, int(os.getenv("AUTOSCALER_COOLDOWN_SECONDS", "30")))
COMPOSE_PROJECT_NAME = os.getenv("AUTOSCALER_COMPOSE_PROJECT", "vortex-core")
COMPOSE_FILE = ROOT_DIR / "infrastructure" / "docker-compose.yml"


def log_event(event_type, **payload):
    print(json.dumps({"event_type": event_type, **payload}, sort_keys=True), flush=True)


class KafkaLagReader:
    def __init__(self):
        self.consumer = KafkaConsumer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            api_version=(0, 10, 1),
            request_timeout_ms=1500,
            connections_max_idle_ms=30000,
        )
        self.admin = KafkaAdminClient(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            client_id="judge_vortex_autoscaler",
        )

    def close(self):
        self.consumer.close()
        self.admin.close()

    def get_route_lag(self, route):
        topic_name = route["topic"]
        consumer_group = route["consumer_group"]
        partitions = self.consumer.partitions_for_topic(topic_name) or set()
        if not partitions:
            return 0

        topic_partitions = [TopicPartition(topic_name, partition) for partition in sorted(partitions)]
        end_offsets = self.consumer.end_offsets(topic_partitions)
        committed_offsets = self.admin.list_consumer_group_offsets(
            consumer_group,
            partitions=topic_partitions,
        )

        lag = 0
        for tp in topic_partitions:
            high_watermark = end_offsets.get(tp, 0) or 0
            committed_meta = committed_offsets.get(tp)
            committed = getattr(committed_meta, "offset", None)
            if committed is None or committed < 0:
                committed = 0
            lag += max(high_watermark - committed, 0)
        return lag


def get_current_replicas(service_name):
    command = [
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={COMPOSE_PROJECT_NAME}",
        "--filter",
        f"label=com.docker.compose.service={service_name}",
        "--format",
        "{{.ID}}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return len(lines)


def scale_service(service_name, replicas):
    command = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT_NAME,
        "up",
        "-d",
        "--scale",
        f"{service_name}={replicas}",
        service_name,
    ]
    subprocess.run(command, check=True, cwd=ROOT_DIR)


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def desired_replicas_for_route(family, lag):
    min_replicas = max(0, env_int(f"AUTOSCALER_MIN_REPLICAS_{family.upper()}", 1))
    max_replicas = max(min_replicas, env_int(f"AUTOSCALER_MAX_REPLICAS_{family.upper()}", 4))
    if lag <= 0:
        return min_replicas
    return min(max_replicas, max(min_replicas, math.ceil(lag / AUTOSCALER_LAG_PER_REPLICA)))


def run_autoscaler(once=False):
    lag_reader = KafkaLagReader()
    last_scaled_at = {}
    try:
        routes = get_executor_routes()
        while True:
            for family, route in routes.items():
                service_name = route["service_name"]
                lag = lag_reader.get_route_lag(route)
                current_replicas = get_current_replicas(service_name)
                desired_replicas = desired_replicas_for_route(family, lag)
                now = time.time()
                cooldown_remaining = AUTOSCALER_COOLDOWN_SECONDS - (now - last_scaled_at.get(service_name, 0))

                log_event(
                    "autoscaler.snapshot",
                    family=family,
                    service=service_name,
                    lag=lag,
                    current_replicas=current_replicas,
                    desired_replicas=desired_replicas,
                )

                if desired_replicas != current_replicas and cooldown_remaining <= 0:
                    scale_service(service_name, desired_replicas)
                    last_scaled_at[service_name] = now
                    log_event(
                        "autoscaler.scale",
                        family=family,
                        service=service_name,
                        lag=lag,
                        previous_replicas=current_replicas,
                        desired_replicas=desired_replicas,
                    )

            if once:
                return
            time.sleep(AUTOSCALER_INTERVAL_SECONDS)
    finally:
        lag_reader.close()


def main():
    parser = argparse.ArgumentParser(description="Scale Judge Vortex executor workers from Kafka lag.")
    parser.add_argument("--once", action="store_true", help="Run a single lag evaluation and exit.")
    args = parser.parse_args()
    run_autoscaler(once=args.once)


if __name__ == "__main__":
    main()
