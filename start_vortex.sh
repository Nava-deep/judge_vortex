#!/bin/bash
set -euo pipefail

# ─── Load .env if present ────────────────────────────────────────────────────
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "--------------------------------------------------------"
echo "  IGNITING JUDGE VORTEX (DOCKER ISOLATE ENGINE)"
echo "--------------------------------------------------------"

# ─── CONFIG ──────────────────────────────────────────────────────────────────
KAFKA_SUBMISSIONS_TOPIC_PARTITIONS="${KAFKA_SUBMISSIONS_TOPIC_PARTITIONS:-8}"
KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-127.0.0.1:9092}"
EXECUTOR_CORE_REPLICAS="${EXECUTOR_CORE_REPLICAS:-1}"
EXECUTOR_JAVA_REPLICAS="${EXECUTOR_JAVA_REPLICAS:-1}"
ENABLE_EXECUTOR_AUTOSCALER="${ENABLE_EXECUTOR_AUTOSCALER:-0}"
MAKE_MIGRATIONS="${MAKE_MIGRATIONS:-0}"
POSTGRES_USER="${POSTGRES_USER:-vortex_admin}"
POSTGRES_DB="${POSTGRES_DB:-judge_vortex_db}"

# ─── IMPORTANT: Capture root dir BEFORE any cd ────────────────────────────────
ROOT_DIR="$(pwd)"
CHECKSUM_FILE="${ROOT_DIR}/.vortex_image_checksum"
CHECKSUM_SOURCES="executor_service/Dockerfile executor_service/sandbox.py executor_service/main.py executor_service/grader.py shared"

_compute_checksum() {
  # Run from ROOT_DIR so paths are always correct regardless of current dir
  (cd "${ROOT_DIR}" && find ${CHECKSUM_SOURCES} -type f 2>/dev/null | sort | xargs md5 -q 2>/dev/null || find ${CHECKSUM_SOURCES} -type f 2>/dev/null | sort | xargs md5sum 2>/dev/null) | md5sum | awk '{print $1}'
}

_images_exist() {
  docker images --format '{{.Repository}}' 2>/dev/null | grep -q "vortex-core"
}

_needs_rebuild() {
  if [ ! -f "${CHECKSUM_FILE}" ]; then
    echo "[build] No previous build record found. Building executor images..."
    return 0
  fi
  local current_checksum saved_checksum
  current_checksum=$(_compute_checksum)
  saved_checksum=$(cat "${CHECKSUM_FILE}")
  if [ "${current_checksum}" != "${saved_checksum}" ]; then
    echo "[build] Executor source files changed. Rebuilding images automatically..."
    return 0
  fi
  if ! _images_exist; then
    echo "[build] Docker images not found locally. Building executor images..."
    return 0
  fi
  return 1
}

_save_checksum() {
  _compute_checksum > "${CHECKSUM_FILE}"
}

# ─── 1. PYTHON DEPENDENCIES ──────────────────────────────────────────────────
echo "[deps] Checking Python dependencies..."
if ! python3 -c "import django, channels, psycopg" >/dev/null 2>&1; then
  echo "[deps] Installing from requirements.txt..."
  python3 -m pip install --user -q -r requirements.txt
fi

# ─── 2. KILL STALE LOCAL PROCESSES ───────────────────────────────────────────
echo "[cleanup] Killing any stale local processes..."
pkill -f "executor_service/main.py" 2>/dev/null || true
pkill -f "manage.py runserver" 2>/dev/null || true
pkill -f "infrastructure/autoscaler/autoscale_executors.py" 2>/dev/null || true

# ─── 3. DOCKER NETWORK ───────────────────────────────────────────────────────
echo "[network] Ensuring vortex-bridge network exists..."
docker network inspect vortex-bridge >/dev/null 2>&1 || docker network create vortex-bridge

# ─── 4. AUTO-DETECT BUILD NEED (must happen BEFORE cd infrastructure) ─────────
_SHOULD_SAVE_CHECKSUM=0
COMPOSE_UP_ARGS=(-d --remove-orphans)

if _needs_rebuild; then
  echo "[build] Removing old executor images to force a clean rebuild..."
  docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
    | grep -E '^vortex-core' \
    | xargs -r docker rmi -f 2>/dev/null || true
  docker builder prune -f --filter "until=24h" >/dev/null 2>&1 || true
  COMPOSE_UP_ARGS+=(--build)
  _SHOULD_SAVE_CHECKSUM=1
else
  echo "[build] Executor images are up to date. Skipping rebuild."
fi

# ─── 5. START ALL SERVICES ───────────────────────────────────────────────────
echo "[docker] Launching core infrastructure (DB, Kafka, Redis, Nginx)..."
CORE_SERVICES=(db zookeeper kafka redis nginx)
EXECUTOR_SERVICES=()
SCALE_ARGS=()

if [ "${EXECUTOR_CORE_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-core)
  SCALE_ARGS+=(--scale "executor-core=${EXECUTOR_CORE_REPLICAS}")
fi
if [ "${EXECUTOR_JAVA_REPLICAS}" -gt 0 ]; then
  EXECUTOR_SERVICES+=(executor-java)
  SCALE_ARGS+=(--scale "executor-java=${EXECUTOR_JAVA_REPLICAS}")
fi

cd "${ROOT_DIR}/infrastructure"

docker compose -p vortex-core up "${COMPOSE_UP_ARGS[@]}" \
  "${SCALE_ARGS[@]}" \
  "${CORE_SERVICES[@]}" \
  "${EXECUTOR_SERVICES[@]}"

echo "[docker] Launching monitoring stack (Prometheus, Grafana)..."
docker compose -f docker-compose.monitor.yml -p vortex-monitor up -d --remove-orphans

cd "${ROOT_DIR}"

# ─── 6. SAVE CHECKSUM (after successful build) ───────────────────────────────
if [ "${_SHOULD_SAVE_CHECKSUM}" = "1" ]; then
  _save_checksum
  echo "[build] New image checksum saved. Next 'make start' will skip rebuild."
fi

# ─── 7. DATABASE MIGRATIONS ──────────────────────────────────────────────────
echo "[db] Waiting for PostgreSQL to be ready..."
until docker exec vortex-postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
  sleep 1
done

if [ "${MAKE_MIGRATIONS}" = "1" ]; then
  echo "[db] Running makemigrations..."
  python3 manage.py makemigrations >/dev/null
fi
echo "[db] Applying migrations..."
python3 manage.py migrate >/dev/null

# ─── 8. REDIS RATE LIMIT RESET ───────────────────────────────────────────────
echo "[redis] Resetting rate-limit counters..."
docker exec vortex-redis redis-cli FLUSHALL >/dev/null 2>&1

# ─── 9. KAFKA TOPIC SETUP ────────────────────────────────────────────────────
echo "[kafka] Waiting for Kafka and ensuring topic topology..."
sleep 6
KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS}" \
KAFKA_SUBMISSIONS_TOPIC_PARTITIONS="${KAFKA_SUBMISSIONS_TOPIC_PARTITIONS}" \
  python3 kafka_setup.py 2>/dev/null

# ─── 10. OPTIONAL AUTOSCALER ─────────────────────────────────────────────────
if [ "${ENABLE_EXECUTOR_AUTOSCALER}" = "1" ]; then
  echo "[autoscaler] Starting executor autoscaler..."
  python3 infrastructure/autoscaler/autoscale_executors.py \
    >/tmp/judge_vortex_autoscaler.log 2>&1 &
fi

# ─── READY ───────────────────────────────────────────────────────────────────
echo ""
echo "========================================================"
echo "  JUDGE VORTEX IS ONLINE"
echo "========================================================"
echo "  App:        http://127.0.0.1:53562"
echo "  Grafana:    http://localhost:3000"
echo "  Prometheus: http://localhost:9090"
echo "  Executors:  core=${EXECUTOR_CORE_REPLICAS}, java=${EXECUTOR_JAVA_REPLICAS}"
if [ "${ENABLE_EXECUTOR_AUTOSCALER}" = "1" ]; then
  echo "  Autoscaler: enabled (log: /tmp/judge_vortex_autoscaler.log)"
fi
echo "========================================================"
echo ""

python3 manage.py runserver 0.0.0.0:53562
