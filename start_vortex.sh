#!/bin/bash

echo "--------------------------------------------------------"
echo "IGNITING JUDGE VORTEX..."
echo "--------------------------------------------------------"

# 1. CLEANUP ORPHANS
echo "Cleaning up local processes..."
pkill -f "executor_service/main.py" 2>/dev/null
pkill -f "manage.py runserver" 2>/dev/null

# Ensures all language folders exist before building
LANGS=("csharp" "java" "rust" "scala")
for L in "${LANGS[@]}"; do
    if [ ! -d "./infrastructure/docker/$L" ]; then
        echo "Creating missing folder: $L"
        mkdir -p "./infrastructure/docker/$L"
    fi
done

# 2. NETWORK CLEANUP
echo "Preparing isolated bridge network..."
docker network inspect vortex-bridge >/dev/null 2>&1 || docker network create vortex-bridge

# 3. REBUILD WARMED IMAGES (SMART BUILD)
echo "Checking Warmed Environments..."
WARM_CONFIG=(
    "csharp:vortex-csharp-warmed" 
    "java:vortex-java-warmed" 
    "rust:vortex-rust-warmed" 
)
for item in "${WARM_CONFIG[@]}"; do
    DIR="${item%%:*}"
    TAG="${item##*:}"
    
    # Check if the image already exists to save time!
    if [[ "$(docker images -q $TAG 2> /dev/null)" == "" ]]; then
        ACTUAL_DIR=$(find ./infrastructure/docker -maxdepth 1 -iname "$DIR" -type d | head -n 1)
        if [ -n "$ACTUAL_DIR" ]; then
            echo "Building $TAG from $ACTUAL_DIR (This may take a minute)..."
            docker build -t "$TAG" "$ACTUAL_DIR" > /dev/null
            if [ $? -eq 0 ]; then
                echo "$TAG ready."
            else
                echo "ERROR: Build failed for $TAG."
            fi
        else
            echo "ERROR: Folder for $DIR not found."
        fi
    else
        echo "$TAG already exists. Skipping build."
    fi
done

# 4. START INFRASTRUCTURE
echo "Launching Core & Monitoring Stacks..."
cd infrastructure
docker-compose -p vortex-core up -d --remove-orphans
docker-compose -f docker-compose.monitor.yml -p vortex-monitor up -d --remove-orphans
cd ..

# 5. DATABASE SYNC & RATE LIMIT RESET
echo "Syncing Database Migrations..."
python3 manage.py makemigrations > /dev/null
python3 manage.py migrate > /dev/null

echo "Resetting user rate-limit history in Redis..."
docker exec vortex-redis redis-cli FLUSHALL > /dev/null 2>&1

# 6. SERVICE BOOT
echo "Starting Executor Sandbox..."
sleep 4 # Give Kafka/Redis a moment to settle
python3 executor_service/main.py &

echo "--------------------------------------------------------"
echo "JUDGE VORTEX IS ONLINE"
echo "Workspace:  http://127.0.0.1:53562"
echo "Grafana:    http://localhost:3000"
echo "Prometheus: http://localhost:9090"
echo "--------------------------------------------------------"

python3 manage.py runserver 53562