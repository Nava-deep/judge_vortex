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

echo "Codespaces profile:"
echo "  core=${EXECUTOR_CORE_REPLICAS}"
echo "  java=${EXECUTOR_JAVA_REPLICAS}"
echo "  swift=${EXECUTOR_SWIFT_REPLICAS}"
echo "  haskell=${EXECUTOR_HASKELL_REPLICAS}"
echo "  csharp=${EXECUTOR_CSHARP_REPLICAS}"
echo
echo "Heavy executors are disabled by default to save Codespaces quota."
echo "Override with EXECUTOR_*_REPLICAS if you need them."
echo

./start_vortex.sh
