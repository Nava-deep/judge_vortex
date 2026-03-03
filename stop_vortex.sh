#!/bin/bash

echo "--------------------------------------------------------"
echo "SHUTTING DOWN JUDGE VORTEX..."
echo "--------------------------------------------------------"

# 1. KILL PROCESSES GRACEFULLY
echo "Stopping Python Services..."
pkill -15 -f "executor_service/main.py" 2>/dev/null
pkill -15 -f "manage.py runserver" 2>/dev/null
sleep 2 

# 2. STOP & REMOVE CONTAINERS (Infra only)
echo "Stopping and cleaning up Docker Stacks..."
if cd infrastructure; then
    docker compose -p vortex-core down
    docker compose -f docker-compose.monitor.yml -p vortex-monitor down
    cd ..
else
    echo "ERROR: Could not find 'infrastructure' directory. Are you running this from the project root?"
fi

# 3. CRASH RECOVERY CLEANUP
# tempfile usually auto-cleans, but this catches anything left behind if the server crashed hard
echo "Clearing orphaned temporary execution files..."
rm -rf /tmp/tmp* 2>/dev/null

echo "--------------------------------------------------------"
echo "SHUTDOWN COMPLETE"
echo "--------------------------------------------------------"