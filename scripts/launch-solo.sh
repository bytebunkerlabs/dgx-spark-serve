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

# Foreground, --rm: Ctrl-C stops and removes it. Host networking so the API is
# on the box's real interfaces (bind carefully — the gateway fronts this).
exec docker run --rm --name serve_solo --network host --gpus all --ipc=host \
  --ulimit nofile=1048576:1048576 \
  -e "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" \
  "${envs[@]}" \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
  -v "$HOME/.triton:/root/.triton" \
  "$IMAGE" \
  vllm serve "$MODEL" --host 127.0.0.1 --port "$API_PORT" "${SERVE_ARGS[@]}"
