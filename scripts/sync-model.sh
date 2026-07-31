#!/usr/bin/env bash
# Replicate one model from the head's HF cache to the worker, over the fabric.
#   scripts/sync-model.sh thinkingmachines/Inkling-Small-NVFP4
#
# Two deliberate choices, both measured:
#   - WORKER_SSH resolves to the fabric IP (/etc/hosts), not the management LAN
#   - aes128-gcm is hardware-accelerated; the default ssh cipher becomes the
#     bottleneck long before a 100 Gb/s link does
# Never run this under sudo — that runs ssh as root, and root has no key.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] && . ./.env
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}
WORKER_SSH=${WORKER_SSH:-spark-2}

id=${1:?usage: sync-model.sh <org/name>}
dir="models--${id//\//--}"
src="$HF_CACHE/hub/$dir"
[ -d "$src" ] || { echo "not in the local cache: $src" >&2; exit 1; }

ssh "$WORKER_SSH" "mkdir -p '$HF_CACHE/hub'"
rsync -ah --info=progress2 -e "ssh -c aes128-gcm@openssh.com" "$src/" "$WORKER_SSH:$src/"

echo
echo "Now verify the copy is complete on BOTH nodes — an incomplete cache fails"
echo "mid-load with a far less obvious error. Shard count must match the index:"
echo "  python3 -c \"import json,glob;i=json.load(open(glob.glob('$src/snapshots/*/model.safetensors.index.json')[0]));print(len(set(i['weight_map'].values())),'shards expected')\""
