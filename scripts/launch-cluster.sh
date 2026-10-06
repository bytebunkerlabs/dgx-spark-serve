#!/usr/bin/env bash
# Launch one model TP=2 across the head and its worker, in the foreground —
# the manual path, for debugging. rack up is the product path: any number of
# nodes, detached, re-formed after a reboot (py/serve.py). Run ON the head.
#   scripts/launch-cluster.sh recipes/<model>.env [--debug]
#
# The mechanics, so you can hold the whole thing in your head:
#   1. one idle keep-alive container per node (sleep infinity as PID 1) — all
#      logs land in `docker logs` because execs redirect to /proc/1/fd/1
#   2. mods (optional per-node patches) applied inside both containers
#   3. the SAME `vllm serve` command runs on both nodes; only four topology
#      flags differ: --nnodes 2 --node-rank N --master-addr --master-port,
#      plus --headless on the worker (engine only, no API server there)
#   4. worker starts first (avoids a rendezvous race), head runs foreground
# This is vLLM's native torch-distributed multi-node path — no Ray. Never pass
# --distributed-executor-backend here.
set -euo pipefail
cd "$(dirname "$0")/.."
. lib/common.sh; . lib/platform.sh; . lib/inventory.sh
load_site_env
rack_resolve_site         # head and worker from the inventory (rack nodes), or .env
[ -n "$WORKER_SSH" ] || { echo "this recipe spans two machines and this rack has one: rack nodes add <worker> --fabric <ip>, or TOPOLOGY=solo in .env for one machine with several GPUs" >&2; exit 1; }
case "$WORKER_NAMES" in *" "*) echo "launch-cluster.sh drives a head and one worker; this rack has workers $WORKER_NAMES" >&2; exit 1 ;; esac
[ -n "${HEAD_IP:-}" ] || { echo "the head has no fabric address: rack nodes add <head> --fabric <ip>, or HEAD_IP in .env" >&2; exit 1; }
WORKER_IP=${WORKER_IP:-192.168.100.2}
FABRIC_IF=${FABRIC_IF:-enp1s0f0np0}
IB_HCAS=${IB_HCAS:-rocep1s0f0,roceP2p1s0f0}
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}
IMAGE=${IMAGE:-dgx-spark-serve:dev}
API_PORT=${API_PORT:-8888}
MASTER_PORT=${MASTER_PORT:-29501}
CTR=serve_node

RECIPE=${1:?usage: launch-cluster.sh recipes/<model>.env [--debug]}
DEBUG=${2:-}
# Recipes are bash: they define MODEL, SERVE_ARGS (array), ENV_EXTRA (array of
# KEY=VAL), MODS (array of dirs). Arrays survive quoting — JSON flags included.
SERVE_ARGS=() ENV_EXTRA=() MODS=()
RECIPE_DIR=${RECIPE_DIR:-$(dirname "$RECIPE")}   # a v2 variant reads "$RECIPE_DIR/model.env"
. "$RECIPE"
: "${MODEL:?recipe must set MODEL}"

# --- sanity ------------------------------------------------------------------
me=$(ip -4 -br addr show "$FABRIC_IF" | awk '{print $3}' | cut -d/ -f1)
[ "$me" = "$HEAD_IP" ] || { echo "this box is ${me:-not on the fabric}, not head $HEAD_IP — run on the head ($HEAD_LABEL)" >&2; exit 1; }
ssh -o BatchMode=yes -o ConnectTimeout=5 "$WORKER_SSH" true \
  || { echo "no passwordless ssh to $WORKER_SSH" >&2; exit 1; }

lid=$(docker image inspect --format '{{.Id}}' "$IMAGE")
rid=$(ssh "$WORKER_SSH" "docker image inspect --format '{{.Id}}' '$IMAGE' 2>/dev/null" || true)
[ "$lid" = "$rid" ] || { echo "image differs between nodes — run scripts/build.sh first" >&2; exit 1; }

for m in "${MODS[@]:-}"; do
  [ -z "$m" ] || [ -d "$m" ] || { echo "mod missing: $m (scripts/fetch-inkling-mod.sh?)" >&2; exit 1; }
done

# Reclaim page cache on both nodes before a big load: on unified memory,
# cached file pages and GPU allocations share one pool. This silently failing
# once wedged a rack, so it warns (docs/12-platforms.md: the sudoers drop-in).
for host in "" "$WORKER_SSH"; do
  ${host:+ssh "$host"} sudo -n /usr/local/sbin/drop-caches 2>/dev/null \
    || ${host:+ssh "$host"} sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
    || echo "WARN: could not drop caches ${host:+on $host }(no passwordless sudo): docs/12-platforms.md" >&2
done

# --- helpers -----------------------------------------------------------------
run_on() { # run_on <node_ip> <cmd...> — locally for head, over ssh for worker
  local ip=$1; shift
  if [ "$ip" = "$HEAD_IP" ]; then "$@"; else ssh "$WORKER_SSH" "$@"; fi
}

env_flags() { # per-node container env. The Spark-specific line is NCCL_IB_HCA:
  local ip=$1 # BOTH RoCE twins of the cabled port, or you cap at one PCIe rail.
  local f=(-e "VLLM_HOST_IP=$ip"
    -e "NCCL_SOCKET_IFNAME=$FABRIC_IF" -e "GLOO_SOCKET_IFNAME=$FABRIC_IF"
    -e "TP_SOCKET_IFNAME=$FABRIC_IF" -e "UCX_NET_DEVICES=$FABRIC_IF"
    -e "NCCL_IB_HCA=$IB_HCAS" -e "NCCL_IB_DISABLE=0"
    -e "NCCL_IGNORE_CPU_AFFINITY=1"
    -e "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
    # local means local: weights come from the mounted cache, vLLM's usage
    # telemetry (stats.vllm.ai) stays off, and the HF hub client never
    # phones home for metadata. Verify anytime with: rack net.
    # A recipe that genuinely needs the hub at runtime can override in
    # ENV_EXTRA — later -e wins.
    -e "HF_HUB_OFFLINE=1" -e "TRANSFORMERS_OFFLINE=1"
    -e "VLLM_NO_USAGE_STATS=1" -e "DO_NOT_TRACK=1")
  # No NCCL_IB_GID_INDEX — ever. NCCL >= 2.21 selects the RoCEv2/IPv4 GID
  # itself when the index is left unset; a stored index rots (docs/00).
  [ "$DEBUG" = "--debug" ] && f+=(-e "NCCL_DEBUG=INFO")
  for kv in "${ENV_EXTRA[@]:-}"; do [ -z "$kv" ] || f+=(-e "$kv"); done
  printf '%q ' "${f[@]}"
}

start_container() { # idle keep-alive; --privileged covers RDMA verbs + memlock
  local ip=$1
  # a previous launch that died mid-start leaves this container behind; remove
  # it rather than failing with a name conflict the user has to clean by hand
  run_on "$ip" docker rm -f "$CTR" >/dev/null 2>&1 || true
  run_on "$ip" docker run -d --rm --name "$CTR" --network host --gpus all \
    --privileged --ipc=host --ulimit nofile=1048576:1048576 --entrypoint= \
    $(env_flags "$ip") \
    -v "$HF_CACHE:/root/.cache/huggingface" \
    -v "$HOME/.cache/vllm:/root/.cache/vllm" \
    -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
    -v "$HOME/.triton:/root/.triton" \
    "$IMAGE" sleep infinity >/dev/null
}

apply_mod() { # copy the mod into a node's container and run its run.sh; fail hard
  local ip=$1 mod=$2 name; name=$(basename "$mod")
  if [ "$ip" = "$HEAD_IP" ]; then
    docker cp "$mod/." "$CTR:/workspace/mods/$name/" 2>/dev/null \
      || { docker exec -w / "$CTR" mkdir -p "/workspace/mods/$name"; docker cp "$mod/." "$CTR:/workspace/mods/$name/"; }
    docker exec "$CTR" bash -c "cd /workspace/mods/$name && chmod +x run.sh && ./run.sh"
  else
    local tmp; tmp=$(ssh "$WORKER_SSH" mktemp -d)
    rsync -a "$mod/" "$WORKER_SSH:$tmp/"
    ssh "$WORKER_SSH" "docker exec -w / $CTR mkdir -p /workspace/mods/$name \
      && docker cp $tmp/. $CTR:/workspace/mods/$name/ \
      && docker exec $CTR bash -c 'cd /workspace/mods/$name && chmod +x run.sh && ./run.sh' \
      && rm -rf $tmp"
  fi
}

node_script() { # the per-node launch script — same serve command, rank differs
  local rank=$1 out=$2
  {
    printf '#!/bin/bash\nset -e\nexec vllm serve %q ' "$MODEL"
    printf '%q ' "${SERVE_ARGS[@]}"
    printf -- '--nnodes 2 --node-rank %s --master-addr %q --master-port %s' \
      "$rank" "$HEAD_IP" "$MASTER_PORT"
    [ "$rank" -gt 0 ] && printf -- ' --headless'
    printf '\n'
  } > "$out"
}

cleanup() {
  echo; echo "== stopping cluster"
  docker stop "$CTR" >/dev/null 2>&1 || true
  ssh "$WORKER_SSH" "docker stop $CTR" >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

# --- go ----------------------------------------------------------------------
echo "== starting idle containers ($IMAGE)"
start_container "$HEAD_IP"
start_container "$WORKER_IP"

# Fail before launch, not during: this image's vLLM must have the native
# multi-node flags (>= 0.26; the NGC 26.07 image's 0.24 predates them).
# Probe EngineArgs, not --help — 0.26.0 accepts --nnodes but hides it there.
docker exec "$CTR" python3 -c \
  "from vllm.engine.arg_utils import EngineArgs; assert hasattr(EngineArgs, 'nnodes')" 2>/dev/null \
  || { echo "this image's vLLM lacks native multi-node (EngineArgs.nnodes). Use --profile upstream, or NVIDIA's Ray playbook path." >&2; cleanup; exit 1; }

for m in "${MODS[@]:-}"; do
  [ -z "$m" ] && continue
  echo "== applying mod $m to both nodes"
  apply_mod "$HEAD_IP" "$m"
  apply_mod "$WORKER_IP" "$m"
done

tmpd=$(mktemp -d)
node_script 1 "$tmpd/worker.sh"
node_script 0 "$tmpd/head.sh"

echo "== launching worker (rank 1, headless, detached)"
rsync -q "$tmpd/worker.sh" "$WORKER_SSH:/tmp/serve-launch.sh"
# /workspace is not in every engine image: make it before copying into it.
ssh "$WORKER_SSH" "docker exec -w / $CTR mkdir -p /workspace \
  && docker cp /tmp/serve-launch.sh $CTR:/workspace/launch.sh \
  && docker exec -d $CTR bash -c 'bash /workspace/launch.sh >> /proc/1/fd/1 2>&1'"

echo "== launching head (rank 0, foreground) — first boot compiles kernels, be patient"
echo "   watch the worker with: ssh $WORKER_SSH docker logs -f $CTR"
docker exec -w / "$CTR" mkdir -p /workspace
docker cp "$tmpd/head.sh" "$CTR:/workspace/launch.sh"
docker exec "$CTR" bash -c 'bash /workspace/launch.sh'
