#!/bin/bash

echo "--------------------------------------------------------"
echo "IGNITING JUDGE VORTEX (NATIVE ENGINE)..."
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
docker-compose -p vortex-core up -d --remove-orphans
docker-compose -f docker-compose.monitor.yml -p vortex-monitor up -d --remove-orphans
cd ..

# 4. DATABASE SYNC & RATE LIMIT RESET
echo "Syncing Database Migrations..."
python3 manage.py makemigrations > /dev/null
python3 manage.py migrate > /dev/null

echo "Resetting user rate-limit history in Redis..."
docker exec vortex-redis redis-cli FLUSHALL > /dev/null 2>&1

# 5. SERVICE BOOT
echo "Starting Native Executor Sandbox..."
sleep 4 # Give Kafka/Redis a moment to settle
python3 executor_service/main.py &

echo "--------------------------------------------------------"
echo "JUDGE VORTEX IS ONLINE"
echo "Workspace:  http://127.0.0.1:53562"
echo "Grafana:    http://localhost:3000"
echo "Prometheus: http://localhost:9090"
echo "--------------------------------------------------------"

python3 manage.py runserver 53562