#!/bin/bash

set -euo pipefail

echo "--------------------------------------------------------"
echo "STARTING JUDGE VORTEX IN GITHUB CODESPACES..."
echo "--------------------------------------------------------"

export EXECUTOR_CORE_REPLICAS="${EXECUTOR_CORE_REPLICAS:-1}"
export EXECUTOR_JAVA_REPLICAS="${EXECUTOR_JAVA_REPLICAS:-1}"
export EXECUTOR_SWIFT_REPLICAS="${EXECUTOR_SWIFT_REPLICAS:-0}"
export EXECUTOR_HASKELL_REPLICAS="${EXECUTOR_HASKELL_REPLICAS:-0}"
export EXECUTOR_CSHARP_REPLICAS="${EXECUTOR_CSHARP_REPLICAS:-0}"
export EXECUTOR_BACKEND="${EXECUTOR_BACKEND:-native}"
export EXECUTOR_MAX_CONCURRENCY="${EXECUTOR_MAX_CONCURRENCY:-4}"

echo "Codespaces profile:"
echo "  core=${EXECUTOR_CORE_REPLICAS}"
echo "  java=${EXECUTOR_JAVA_REPLICAS}"
echo "  backend=${EXECUTOR_BACKEND}"
echo "  max_concurrency=${EXECUTOR_MAX_CONCURRENCY}"
echo
echo "Codespaces defaults to the faster native executor backend."
echo "Override with EXECUTOR_BACKEND=isolate if you want to test isolate there."
echo

echo "Checking Python dependencies..."
if ! python3 -c "import django, channels, psycopg" >/dev/null 2>&1; then
  echo "Syncing Python packages from requirements.txt..."
  pip3 install --user -r requirements.txt
  echo
fi

./start_vortex.sh
