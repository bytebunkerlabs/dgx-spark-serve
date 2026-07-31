#!/usr/bin/env bash
# Stop serving on both nodes. Containers run --rm, so stop == cleanup.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && . ./.env
WORKER_SSH=${WORKER_SSH:-spark-2}
docker stop serve_node serve_solo 2>/dev/null || true
ssh "$WORKER_SSH" "docker stop serve_node" 2>/dev/null || true
echo "stopped"
