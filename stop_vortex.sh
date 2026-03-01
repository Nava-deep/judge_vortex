#!/bin/bash

echo "--------------------------------------------------------"
echo "🛑 SHUTTING DOWN JUDGE VORTEX..."
echo "--------------------------------------------------------"

# 1. KILL PROCESSES GRACEFULLY
echo "Stopping Python Services..."
pkill -15 -f "executor_service/main.py" 2>/dev/null
pkill -15 -f "manage.py runserver" 2>/dev/null
sleep 2 

# 2. STOP & REMOVE CONTAINERS
echo "Stopping and cleaning up Docker Stacks..."
if cd infrastructure; then
    # 'down' completely removes the containers and networks
    docker compose -p vortex-core down
    docker compose -f docker-compose.monitor.yml -p vortex-monitor down
    cd ..
else
    echo "❌ ERROR: Could not find 'infrastructure' directory. Are you running this from the project root?"
fi

# 3. DISK CLEANUP (Workspaces)
SPACE_BEFORE=$(du -sh /tmp 2>/dev/null | cut -f1)
echo "Cleaning temporary workspaces (/tmp/vortex_*)..."
rm -rf /tmp/vortex_*
SPACE_AFTER=$(du -sh /tmp 2>/dev/null | cut -f1)
echo "Saved Temp Space: $SPACE_BEFORE -> $SPACE_AFTER"

# 4. IMAGE CLEANUP (Warmed Compilers)
echo "🗑️ Deleting warmed compiler images to free up space..."
WARM_TAGS=("vortex-csharp-warmed" "vortex-java-warmed" "vortex-rust-warmed" "vortex-scala-warmed")

for TAG in "${WARM_TAGS[@]}"; do
    # Check if the image exists before trying to delete it
    if [[ "$(docker images -q $TAG 2> /dev/null)" != "" ]]; then
        echo "   Removing $TAG..."
        docker rmi -f "$TAG" > /dev/null 2>&1
    fi
done

echo "--------------------------------------------------------"
echo "💤 SHUTDOWN COMPLETE"
echo "--------------------------------------------------------"