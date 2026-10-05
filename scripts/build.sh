#!/usr/bin/env bash
# Build the image on the head and sync it to every worker, byte-identically.
#   scripts/build.sh                      # community Spark vLLM (the default, as in rack build)
#   scripts/build.sh --profile ngc        # NVIDIA's NGC vLLM
#   scripts/build.sh --profile upstream   # upstream vLLM (Inkling-capable)
#   scripts/build.sh --no-sync            # build only
# Run this ON the head: the image must match its architecture, and docker
# build there is native.
set -euo pipefail
cd "$(dirname "$0")/.."
. lib/common.sh; . lib/platform.sh; . lib/inventory.sh
load_site_env
IMAGE=${IMAGE:-dgx-spark-serve:dev}
rack_resolve_workers

PROFILE=community SYNC=1
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE=$2; shift 2 ;;
    --no-sync) SYNC=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$PROFILE" in
  ngc)      BASE=nvcr.io/nvidia/vllm:26.07-py3; EXTRAS=0 ;;
  upstream) BASE=vllm/vllm-openai:v0.26.0-aarch64-cu129-ubuntu2404; EXTRAS=1 ;;
  community) BASE=eugr/spark-vllm:latest; EXTRAS=0 ;;
  *) echo "profile must be ngc, upstream, or community" >&2; exit 2 ;;
esac

echo "== building $IMAGE from $BASE (profile: $PROFILE)"
[ "$PROFILE" = community ] && docker pull "$BASE" && docker inspect --format 'community base digest: {{index .RepoDigests 0}}' "$BASE"
docker build --build-arg BASE_IMAGE="$BASE" --build-arg INSTALL_EXTRAS="$EXTRAS" -t "$IMAGE" .

[ "$SYNC" = 1 ] || exit 0
[ -n "$WORKER_NAMES" ] || { echo "== done: $IMAGE (no workers to sync to)"; exit 0; }

# --- sync to each worker: docker save | ssh | docker load, skipped when already identical
local_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")
for n in $WORKER_NAMES; do
  target=$(node_ssh "$n")
  remote_id=$(ssh -n "$target" "docker image inspect --format '{{.Id}}' '$IMAGE' 2>/dev/null" || true)
  if [ "$local_id" = "$remote_id" ]; then
    echo "== $n already has $IMAGE ($local_id) — skipping sync"
  else
    echo "== syncing $IMAGE to $n (this moves the whole image over the fabric)"
    docker save "$IMAGE" | ssh -c aes128-gcm@openssh.com "$target" "docker load"
  fi
done
echo "== done. Image on every node: $IMAGE"
