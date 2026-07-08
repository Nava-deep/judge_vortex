#!/bin/bash

echo "--------------------------------------------------------"
echo "  SHUTTING DOWN JUDGE VORTEX"
echo "--------------------------------------------------------"

# ─── 1. KILL HOST PROCESSES GRACEFULLY ───────────────────────────────────────
echo "[cleanup] Stopping Python services..."
pkill -15 -f "executor_service/main.py" 2>/dev/null || true
pkill -15 -f "manage.py runserver" 2>/dev/null || true
pkill -15 -f "infrastructure/autoscaler/autoscale_executors.py" 2>/dev/null || true
sleep 2

# ─── 2. STOP DOCKER STACKS ───────────────────────────────────────────────────
echo "[docker] Stopping Docker stacks..."
if cd infrastructure; then
  docker compose -p vortex-core down
  docker compose -f docker-compose.monitor.yml -p vortex-monitor down
  cd ..
else
  echo "ERROR: Could not find 'infrastructure' directory. Run from project root."
fi

# ─── 3. OPTIONAL: WIPE IMAGES (pass --wipe to force rebuild next start) ──────
if [ "${1:-}" = "--wipe" ]; then
  echo "[wipe] Removing executor Docker images..."
  docker images --format '{{.Repository}}:{{.Tag}}' \
    | grep -E '^vortex-core' \
    | xargs -r docker rmi -f 2>/dev/null || true
  docker builder prune -f >/dev/null 2>&1 || true
  # Delete checksum so next 'make start' triggers a fresh build
  rm -f .vortex_image_checksum
  echo "[wipe] Images removed. Next 'make start' will do a clean rebuild."
fi

# ─── 4. CLEAR TEMP FILES ─────────────────────────────────────────────────────
echo "[cleanup] Clearing orphaned temp execution files..."
rm -rf /tmp/tmp* 2>/dev/null || true

echo "--------------------------------------------------------"
echo "  SHUTDOWN COMPLETE"
echo "--------------------------------------------------------"
