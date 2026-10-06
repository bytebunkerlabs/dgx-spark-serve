#!/usr/bin/env bash
# Stop the containers the manual launchers start, and the site's other engines
# (FOREIGN_ENGINES), on every node. rack down also stops what rack up started.
set -u
cd "$(dirname "$0")/.." || exit 1
. lib/common.sh; . lib/platform.sh; . lib/inventory.sh
load_site_env
rack_resolve_workers      # the inventory's workers; none on a single-node site
# shellcheck disable=SC2086  # FOREIGN_ENGINES is a list of container names
docker rm -f serve_node serve_solo ${FOREIGN_ENGINES:-} >/dev/null 2>&1 || true
for n in $WORKER_NAMES; do
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$(node_ssh "$n")" "docker rm -f serve_node ${FOREIGN_ENGINES:-}" >/dev/null 2>&1 || true
done
echo "stopped"
