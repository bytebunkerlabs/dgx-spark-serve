#!/usr/bin/env bash
# Replicate one model from the head's HF cache to every worker, over the fabric.
#   scripts/sync-model.sh thinkingmachines/Inkling-Small-NVFP4
#
# Two deliberate choices, both measured:
#   - WORKER_SSH resolves to the fabric IP (/etc/hosts), not the management LAN
#   - aes128-gcm is hardware-accelerated; the default ssh cipher becomes the
#     bottleneck long before a 100 Gb/s link does
# Never run this under sudo — that runs ssh as root, and root has no key.
set -eu
cd "$(dirname "$0")/.."
. lib/common.sh; . lib/platform.sh; . lib/inventory.sh
load_site_env
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}
rack_resolve_workers

id=${1:?usage: sync-model.sh <org/name>}
dir="models--${id//\//--}"
src="$HF_CACHE/hub/$dir"
[ -d "$src" ] || { echo "not in the local cache: $src" >&2; exit 1; }
[ -n "$WORKER_NAMES" ] || { echo "no workers in this rack: nothing to replicate"; exit 0; }

for n in $WORKER_NAMES; do
  target=$(node_ssh "$n") dst="$(node_hf_cache "$n")/hub"
  echo "== $n"
  ssh -n "$target" "mkdir -p $(printf %q "$dst")"
  rsync -ah --info=progress2 -e "ssh -c aes128-gcm@openssh.com" "$src/" "$target:$dst/$dir/"
done

echo
echo "Now verify the copy is complete on EVERY node — an incomplete cache fails"
echo "mid-load with a far less obvious error. Shard count must match the index:"
echo "  python3 -c \"import json,glob;i=json.load(open(glob.glob('$src/snapshots/*/model.safetensors.index.json')[0]));print(len(set(i['weight_map'].values())),'shards expected')\""
