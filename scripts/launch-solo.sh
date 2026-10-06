#!/usr/bin/env bash
# Serve one model on THIS node only, in the foreground — the manual path, for
# debugging a recipe. rack up is the product path: detached, restarted with
# the machine, keyed, memory-capped and watched (py/serve.py).
#   scripts/launch-solo.sh recipes/phase1-qwen3-8b/dgx.env
set -euo pipefail
cd "$(dirname "$0")/.."
. lib/common.sh
load_site_env
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}
IMAGE=${IMAGE:-dgx-spark-serve:dev}
API_PORT=${API_PORT:-8888}

RECIPE=${1:?usage: launch-solo.sh recipes/<model>.env}
SERVE_ARGS=() ENV_EXTRA=() MODS=()
RECIPE_DIR=${RECIPE_DIR:-$(dirname "$RECIPE")}   # a v2 variant reads "$RECIPE_DIR/model.env"
# shellcheck source=/dev/null
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

# On unified memory, cached file pages and GPU allocations share one pool —
# reclaim before a big load. A sudoers drop-in may allow exactly
# /usr/local/sbin/drop-caches (docs/12-platforms.md); this silently failing
# once wedged a rack, so it warns.
sudo -n /usr/local/sbin/drop-caches 2>/dev/null \
  || sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
  || echo "WARN: could not drop caches (no passwordless sudo): docs/12-platforms.md" >&2

# Foreground, --rm: Ctrl-C stops and removes it. Host networking so the API is
# on the box's real interfaces (bind carefully — the gateway fronts this).
# journald keeps the log after --rm deletes the container — a crash trace
# must outlive the thing that crashed. Read old runs with:
#   journalctl CONTAINER_NAME=serve_solo --since "2 hours ago"
# --entrypoint= : upstream vllm-openai images ship ENTRYPOINT ["vllm","serve"],
# so without clearing it the command becomes `vllm serve vllm serve <model>`
# ("unrecognized arguments", measured on v0.27.1).
exec docker run --rm --name serve_solo --network host --gpus all --ipc=host \
  --log-driver journald \
  --ulimit nofile=1048576:1048576 \
  --entrypoint= \
  -e "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" \
  -e "HF_HUB_OFFLINE=1" -e "TRANSFORMERS_OFFLINE=1" \
  -e "VLLM_NO_USAGE_STATS=1" -e "DO_NOT_TRACK=1" \
  "${envs[@]}" \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  ${mounts[@]+"${mounts[@]}"} \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
  -v "$HOME/.triton:/root/.triton" \
  "$IMAGE" \
  vllm serve "$MODEL" --host 127.0.0.1 --port "$API_PORT" "${SERVE_ARGS[@]}"
