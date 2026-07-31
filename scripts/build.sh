#!/usr/bin/env bash
# Build the image on spark-1 and sync it to the worker, byte-identically.
#   scripts/build.sh --profile ngc        # phases 1-2 (default)
#   scripts/build.sh --profile upstream   # phase 3 (Inkling-capable vLLM)
#   scripts/build.sh --no-sync            # build only
# Run this ON spark-1 — the image must be aarch64 and docker build here is native.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && . ./.env
IMAGE=${IMAGE:-dgx-spark-serve:dev}
WORKER_SSH=${WORKER_SSH:-spark-2}

PROFILE=ngc SYNC=1
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

# --- sync to worker: docker save | ssh | docker load, skipped when already identical
local_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")
remote_id=$(ssh "$WORKER_SSH" "docker image inspect --format '{{.Id}}' '$IMAGE' 2>/dev/null" || true)
if [ "$local_id" = "$remote_id" ]; then
  echo "== worker already has $IMAGE ($local_id) — skipping sync"
else
  echo "== syncing $IMAGE to $WORKER_SSH (this moves the whole image over the fabric)"
  docker save "$IMAGE" | ssh -c aes128-gcm@openssh.com "$WORKER_SSH" "docker load"
fi
echo "== done. Image on both nodes: $IMAGE"
