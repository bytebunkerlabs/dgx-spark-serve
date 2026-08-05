#!/usr/bin/env bash
# Serve one model on THIS node only. The smallest end-to-end path — phase 1.
#   scripts/launch-solo.sh recipes/phase1-qwen3-8b.env
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && . ./.env
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}
IMAGE=${IMAGE:-dgx-spark-serve:dev}
API_PORT=${API_PORT:-8000}

RECIPE=${1:?usage: launch-solo.sh recipes/<model>.env}
SERVE_ARGS=() ENV_EXTRA=() MODS=()
. "$RECIPE"
: "${MODEL:?recipe must set MODEL}"

envs=()
for kv in "${ENV_EXTRA[@]:-}"; do [ -z "$kv" ] || envs+=(-e "$kv"); done

# Mods, solo flavor: cluster mode execs a run.sh inside a live container, but
# solo is one docker run — so a mod's overlay/ tree is bind-mounted file by
# file over the image, read-only. overlay/ mirrors the container filesystem.
mounts=()
for m in "${MODS[@]:-}"; do
  [ -z "$m" ] && continue
  [ -d "$m/overlay" ] || { echo "mod has no overlay/ dir: $m" >&2; exit 1; }
  while IFS= read -r f; do
    mounts+=(-v "$PWD/$f:${f#"$m"/overlay}:ro")
  done < <(find "$m/overlay" -type f)
done

# Best effort, same as cluster: on unified memory, cached file pages and GPU
# allocations share one pool — reclaim before a big load.
sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
  || echo "note: could not drop caches (no passwordless sudo) — fine unless memory is tight"

# Foreground, --rm: Ctrl-C stops and removes it. Host networking so the API is
# on the box's real interfaces (bind carefully — the gateway fronts this).
# journald keeps the log after --rm deletes the container — a crash trace
# must outlive the thing that crashed. Read old runs with:
#   journalctl CONTAINER_NAME=serve_solo --since "2 hours ago"
exec docker run --rm --name serve_solo --network host --gpus all --ipc=host \
  --log-driver journald \
  --ulimit nofile=1048576:1048576 \
  -e "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" \
  "${envs[@]}" \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  ${mounts[@]+"${mounts[@]}"} \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
  -v "$HOME/.triton:/root/.triton" \
  "$IMAGE" \
  vllm serve "$MODEL" --host 127.0.0.1 --port "$API_PORT" "${SERVE_ARGS[@]}"
