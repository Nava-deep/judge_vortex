#!/bin/bash

echo "--------------------------------------------------------"
echo "IGNITING JUDGE VORTEX (DOCKER ISOLATE ENGINE)..."
echo "--------------------------------------------------------"

# 1. CLEANUP ORPHANS
echo "Cleaning up local processes..."
pkill -f "executor_service/main.py" 2>/dev/null
pkill -f "manage.py runserver" 2>/dev/null

# 2. NETWORK PREP
echo "Preparing isolated bridge network..."
docker network inspect vortex-bridge >/dev/null 2>&1 || docker network create vortex-bridge

# 3. START INFRASTRUCTURE (Kafka, Redis, Grafana, etc.)
echo "Launching Core & Monitoring Stacks..."
cd infrastructure
KAFKA_SUBMISSIONS_TOPIC_PARTITIONS="${KAFKA_SUBMISSIONS_TOPIC_PARTITIONS:-8}"
EXECUTOR_CORE_REPLICAS="${EXECUTOR_CORE_REPLICAS:-1}"
EXECUTOR_JAVA_REPLICAS="${EXECUTOR_JAVA_REPLICAS:-1}"
EXECUTOR_SWIFT_REPLICAS="${EXECUTOR_SWIFT_REPLICAS:-1}"
EXECUTOR_HASKELL_REPLICAS="${EXECUTOR_HASKELL_REPLICAS:-1}"
EXECUTOR_CSHARP_REPLICAS="${EXECUTOR_CSHARP_REPLICAS:-1}"
FORCE_BUILD="${FORCE_BUILD:-0}"
MAKE_MIGRATIONS="${MAKE_MIGRATIONS:-0}"
CORE_SERVICES=(db zookeeper kafka redis nginx)
EXECUTOR_SERVICES=()
SCALE_ARGS=()
COMPOSE_UP_ARGS=(-d --remove-orphans)

if [ "${FORCE_BUILD}" = "1" ]; then
  echo "Rebuilding executor images..."
  COMPOSE_UP_ARGS+=(--build)
else
  echo "Reusing existing executor images. Set FORCE_BUILD=1 to rebuild."
fi

if [ "${EXECUTOR_CORE_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-core)
  SCALE_ARGS+=(--scale "executor-core=${EXECUTOR_CORE_REPLICAS}")
fi
if [ "${EXECUTOR_JAVA_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-java)
  SCALE_ARGS+=(--scale "executor-java=${EXECUTOR_JAVA_REPLICAS}")
fi
if [ "${EXECUTOR_SWIFT_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-swift)
  SCALE_ARGS+=(--scale "executor-swift=${EXECUTOR_SWIFT_REPLICAS}")
fi
if [ "${EXECUTOR_HASKELL_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-haskell)
  SCALE_ARGS+=(--scale "executor-haskell=${EXECUTOR_HASKELL_REPLICAS}")
fi
if [ "${EXECUTOR_CSHARP_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-csharp)
  SCALE_ARGS+=(--scale "executor-csharp=${EXECUTOR_CSHARP_REPLICAS}")
fi

docker compose -p vortex-core up "${COMPOSE_UP_ARGS[@]}" \
  "${SCALE_ARGS[@]}" \
  "${CORE_SERVICES[@]}" \
  "${EXECUTOR_SERVICES[@]}"
docker compose -f docker-compose.monitor.yml -p vortex-monitor up -d --remove-orphans
cd ..

# 4. DATABASE SYNC & RATE LIMIT RESET
echo "Syncing Database Migrations..."
if [ "${MAKE_MIGRATIONS}" = "1" ]; then
  python3 manage.py makemigrations > /dev/null
fi
python3 manage.py migrate > /dev/null

echo "Resetting user rate-limit history in Redis..."
docker exec vortex-redis redis-cli FLUSHALL > /dev/null 2>&1

# 5. SERVICE BOOT
echo "Ensuring Kafka topic topology..."
sleep 6
KAFKA_SUBMISSIONS_TOPIC_PARTITIONS="${KAFKA_SUBMISSIONS_TOPIC_PARTITIONS}" python3 kafka_setup.py

echo "--------------------------------------------------------"
echo "JUDGE VORTEX IS ONLINE"
echo "Workspace:  http://127.0.0.1:53562"
echo "Executors:  core=${EXECUTOR_CORE_REPLICAS}, java=${EXECUTOR_JAVA_REPLICAS}, swift=${EXECUTOR_SWIFT_REPLICAS}, haskell=${EXECUTOR_HASKELL_REPLICAS}, csharp=${EXECUTOR_CSHARP_REPLICAS}"
echo "Grafana:    http://localhost:3000"
echo "Prometheus: http://localhost:9090"
echo "--------------------------------------------------------"

python3 manage.py runserver 53562
