#!/usr/bin/env bash
# Stop serving on every node. Containers run --rm, so stop == cleanup.
set -u
cd "$(dirname "$0")/.." || exit 1
. lib/common.sh; . lib/platform.sh; . lib/inventory.sh
load_site_env
rack_resolve_workers      # the inventory's workers; none on a single-node site
docker stop serve_node serve_solo 2>/dev/null || true
for n in $WORKER_NAMES; do
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$(node_ssh "$n")" "docker stop serve_node" >/dev/null 2>&1 || true
done
echo "stopped"
