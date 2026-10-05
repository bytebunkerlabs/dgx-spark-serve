#!/usr/bin/env bash
# rack monitor: one telemetry endpoint for the whole rack.
#
#   rack monitor up              build, ship to the worker, (re)start on every node
#   rack monitor down            stop and remove the monitor containers (token kept)
#   rack monitor status          containers, health, and the endpoint to add
#   rack monitor token [--rotate]  print the token, or replace it and restart
#   rack monitor logs [worker]   the monitor's own log
#
# Run it on the head. Every node gets two containers from one small image:
#
#   rack-monitor         host network and host pids so it can see the node,
#                        but read-only, no capabilities, your uid, 256 MB.
#                        Samples every 2 s, serves :9177 behind one token.
#   rack-monitor-docker  no network at all. The only thing holding the Docker
#                        socket; it writes the container list to a file the
#                        monitor reads. The network-facing process never
#                        talks to Docker.
#
# The head's monitor also asks the worker's over the fabric, so the app needs
# one endpoint and one token for the whole rack.  docs/11-monitor.md
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
HEAD_IP=${HEAD_IP:-192.168.100.1}
WORKER_IP=${WORKER_IP:-192.168.100.2}
HEAD_LABEL=${HEAD_LABEL:-${HEAD_SSH:-spark-1}}
WORKER_SSH=${WORKER_SSH-spark-2}             # empty: a single-node site
MONITOR_PORT=${MONITOR_PORT:-9177}
MONITOR_CLUSTER=${MONITOR_CLUSTER:-rack}
MONITOR_ENGINE_PORTS=${MONITOR_ENGINE_PORTS:-}
MONITOR_BIND=${MONITOR_BIND:-0.0.0.0}        # the head's listen addresses; the worker binds its fabric IP
TOKEN_FILE=$HOME/.config/rack/monitor.token  # same path on every node
STATE_DIR=$HOME/.local/state/rack-monitor
TAG=$(cat "$HERE/rackmon.py" "$HERE/Dockerfile" | sha256sum | cut -c1-12)
IMAGE=rack-monitor:$TAG

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# rack passes RACK_IS_HEAD (its inventory knows); run directly, owning the
# head's fabric IP is the test.
on_head() {
  [ -n "${RACK_IS_HEAD:-}" ] && { [ "$RACK_IS_HEAD" = 1 ]; return; }
  { ip -o addr show 2>/dev/null || true; } | grep -q " $HEAD_IP/"
}
have_worker() { [ -n "$WORKER_SSH" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 "$WORKER_SSH" true 2>/dev/null; }
need_head() {
  on_head || die "rack monitor runs on the head ($HEAD_LABEL): ssh $HEAD_LABEL, then rack monitor ${1:-up}"
  command -v docker >/dev/null || die "docker not found on $(hostname)"
}

ensure_token() {
  [ -s "$TOKEN_FILE" ] && return 0
  mkdir -p "$(dirname "$TOKEN_FILE")"
  ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE" )
  bold "monitor: new token in $TOKEN_FILE (rack monitor token prints it)"
}

build_image() {
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    dim "monitor: image $IMAGE already built"
  else
    bold "monitor: building $IMAGE"
    docker build -q -t "$IMAGE" -t rack-monitor:latest "$HERE" >/dev/null
  fi
}

ship_worker() {
  local remote
  remote=$(ssh -n "$WORKER_SSH" "docker image inspect --format '{{.Id}}' '$IMAGE' 2>/dev/null" || true)
  if [ -z "$remote" ]; then
    bold "monitor: shipping $IMAGE to $WORKER_SSH"
    docker save "$IMAGE" | ssh "$WORKER_SSH" "docker load -q" >/dev/null
    ssh -n "$WORKER_SSH" "docker tag '$IMAGE' rack-monitor:latest"
  fi
  # same token on every node: the head presents it when it asks the worker
  ssh "$WORKER_SSH" "umask 077; mkdir -p ~/.config/rack && cat > ~/.config/rack/monitor.token" < "$TOKEN_FILE"
}

# The per-node start script. Positional args keep ssh quoting out of it.
node_script() {
  cat <<'SH'
set -euo pipefail
image=$1 name=$2 role=$3 peers=$4 port=$5 cluster=$6 engine_ports=$7 bind=$8
tok=$HOME/.config/rack/monitor.token
state=$HOME/.local/state/rack-monitor
[ -s "$tok" ] || { echo "no token at $tok" >&2; exit 1; }
mkdir -p "$state" && chmod 700 "$state"
docker rm -f rack-monitor rack-monitor-docker >/dev/null 2>&1 || true
uid=$(id -u) gid=$(id -g) dgid=$(stat -c %g /var/run/docker.sock)
docker run -d --name rack-monitor-docker --restart unless-stopped \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$uid:$dgid" --memory 64m --pids-limit 16 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$state":/run/rackmon \
  --label ai.bytebunker.rack-monitor=relay \
  "$image" docker-relay >/dev/null
# --gpus all is how rack's own engines get the GPU (whether or not Docker lists
# a runtime named nvidia); without a GPU stack the monitor still runs, GPU-less.
gpu=()
command -v nvidia-smi >/dev/null 2>&1 && gpu=(--gpus all)
extra=()
[ -n "$engine_ports" ] && extra=(-e "MONITOR_ENGINE_PORTS=$engine_ports")
run_monitor() {
docker run -d --name rack-monitor --restart unless-stopped \
  --network host --pid host --cgroupns host "$@" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$uid:$gid" --memory 256m --pids-limit 64 \
  -e NVIDIA_DRIVER_CAPABILITIES=utility \
  -e "MONITOR_NAME=$name" -e "MONITOR_ROLE=$role" -e "MONITOR_PEERS=$peers" \
  -e "MONITOR_PORT=$port" -e "MONITOR_BIND=$bind" -e "MONITOR_CLUSTER=$cluster" "${extra[@]}" \
  -v "$tok":/run/secrets/rack-monitor-token:ro \
  -v "$state":/run/rackmon:ro \
  -v /etc/os-release:/run/host/os-release:ro \
  --label ai.bytebunker.rack-monitor=monitor \
  "$image" serve >/dev/null
}
if ! run_monitor "${gpu[@]}" 2>/dev/null; then
  docker rm -f rack-monitor >/dev/null 2>&1 || true
  run_monitor
  echo "started (without the GPU: docker refused --gpus all)"
  exit 0
fi
echo "started${gpu:+ with the GPU}"
SH
}

start_node() {   # start_node <local|ssh-target> <name> <role> <peers> <bind>
  local where=$1 name=$2 role=$3 peers=$4 bind=$5 args
  args=$(printf '%q ' "$IMAGE" "$name" "$role" "$peers" "$MONITOR_PORT" "$MONITOR_CLUSTER" "$MONITOR_ENGINE_PORTS" "$bind")
  if [ "$where" = local ]; then
    node_script | eval "bash -s -- $args"
  else
    node_script | ssh "$where" "bash -s -- $args"
  fi
}

prune_images() {   # earlier builds of the monitor, on every node; :latest and the running tag stay
  local prune="docker images rack-monitor --format '{{.Tag}}' | grep -vx -e '$TAG' -e latest | sed 's/^/rack-monitor:/' | xargs -r docker rmi >/dev/null 2>&1 || true"
  bash -c "$prune"
  have_worker && ssh -n "$WORKER_SSH" "$prune" || true
}

# Ask the local monitor through Python so the token never sits on a command line.
query() {   # query <path>  -> JSON on stdout
  MON_URL="http://127.0.0.1:$MONITOR_PORT$1" MON_TOKEN_FILE="$TOKEN_FILE" python3 - <<'PY'
import json, os, sys, urllib.request
tok = open(os.environ["MON_TOKEN_FILE"]).read().strip()
req = urllib.request.Request(os.environ["MON_URL"], headers={"Authorization": "Bearer " + tok})
with urllib.request.urlopen(req, timeout=6) as r:
    sys.stdout.write(r.read().decode())
PY
}

summary() {   # one line per node from /v1/cluster
  query /v1/cluster | python3 -c '
import json, sys
d = json.load(sys.stdin)
bad = 0
for n in d["nodes"]:
    if not n.get("ok"):
        bad += 1
        print("  %-10s DOWN  %s" % (n.get("name"), n.get("error", "")))
        continue
    g = (n.get("gpus") or [{}])[0]
    m = n.get("mem") or {}
    eng = ", ".join("%s:%s" % (e.get("kind"), e.get("port")) for e in n.get("engines") or []) or "no engine"
    used = (m.get("used") or 0) / 2**30; total = (m.get("total") or 0) / 2**30
    print("  %-10s ok    %-6s  gpu %s%%  mem %.0f/%.0f GiB  %s  %d containers%s" % (
        n["name"], n.get("role"), g.get("util", "-"), used, total, eng,
        len(n.get("containers") or []),
        ("  (" + n["containers_error"] + ")") if n.get("containers_error") else ""))
sys.exit(1 if bad else 0)'
}

endpoints() {
  local ts lan dns cidr dev code
  ts=$(tailscale ip -4 2>/dev/null | head -1 || true)
  dns=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))
except Exception: pass' 2>/dev/null || true)
  dev=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  lan=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
  echo
  bold "add this to ByteBunker: Cluster > Add monitor"
  [ -n "$ts" ]  && printf '  tailnet   http://%s:%s\n' "$ts" "$MONITOR_PORT"
  [ -n "$dns" ] && printf '            http://%s:%s\n' "$dns" "$MONITOR_PORT"
  if [ -n "$lan" ]; then
    printf '  LAN       http://%s:%s' "$lan" "$MONITOR_PORT"
    # Measure, from the worker, whether the LAN path is open. ufw drops it by
    # default; Docker-published ports (LiteLLM's) bypass ufw, host-network ones don't.
    code=""
    if have_worker; then
      code=$(ssh -n "$WORKER_SSH" "curl -s -m 3 -o /dev/null -w '%{http_code}' http://$lan:$MONITOR_PORT/v1/hello" 2>/dev/null || true)
    fi
    if [ "$code" = 200 ]; then
      echo "   (open)"
    elif [ -n "$code" ]; then
      cidr=$(ip -o -4 addr show dev "$dev" 2>/dev/null | awk '{print $4}' | head -1)
      echo "   (closed by the firewall; to open it on the LAN only:"
      echo "             sudo ufw allow from $(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$cidr") to any port $MONITOR_PORT proto tcp)"
    else
      echo "   (not measured: no worker to test from)"
    fi
  fi
  printf '  token     rack monitor token\n'
}

cmd_up() {
  need_head up
  ensure_token
  build_image
  local peers=""
  if have_worker; then
    ship_worker
    peers="$WORKER_SSH=http://$WORKER_IP:$MONITOR_PORT"
    # the worker answers only the head, over the fabric (and itself)
    printf 'monitor: %-10s ' "$WORKER_SSH"; start_node "$WORKER_SSH" "$WORKER_SSH" worker "" "$WORKER_IP,127.0.0.1"
  elif [ -n "$WORKER_SSH" ]; then
    dim "monitor: $WORKER_SSH unreachable over ssh; the head will report it down until rack monitor up runs again"
    peers="$WORKER_SSH=http://$WORKER_IP:$MONITOR_PORT"
  fi
  printf 'monitor: %-10s ' "$HEAD_LABEL"; start_node local "$HEAD_LABEL" head "$peers" "${MONITOR_BIND:-0.0.0.0}"
  prune_images
  local i
  for i in $(seq 1 15); do
    sleep 2
    if summary >/dev/null 2>&1; then break; fi
  done
  bold "monitor: first samples"
  summary || dim "  (a node is still starting or unreachable: rack monitor status in a minute)"
  endpoints
}

cmd_down() {
  need_head down
  docker rm -f rack-monitor rack-monitor-docker >/dev/null 2>&1 || true
  echo "monitor: $HEAD_LABEL stopped"
  if have_worker; then
    ssh -n "$WORKER_SSH" "docker rm -f rack-monitor rack-monitor-docker >/dev/null 2>&1 || true"
    echo "monitor: $WORKER_SSH stopped"
  fi
  dim "token kept in $TOKEN_FILE; rack monitor up brings it back with the same one"
}

cmd_status() {
  need_head status
  bold "containers"
  docker ps -a --filter label=ai.bytebunker.rack-monitor --format '  {{.Names}}\t{{.Status}}\t{{.Image}}' | sed "s/^/  $HEAD_LABEL/" || true
  if have_worker; then
    ssh -n "$WORKER_SSH" "docker ps -a --filter label=ai.bytebunker.rack-monitor --format '  {{.Names}}\t{{.Status}}\t{{.Image}}'" | sed "s/^/  $WORKER_SSH/" || true
  fi
  echo; bold "nodes"
  summary || true
  endpoints
}

cmd_brief() {   # one line for `rack status`
  on_head || { dim "  (run on the head to see the monitor)"; return 0; }
  if ! docker ps --format '{{.Names}}' | grep -qx rack-monitor; then
    dim "  not running: rack monitor up"; return 0
  fi
  summary 2>/dev/null || dim "  running, not answering yet: rack monitor status"
}

cmd_token() {
  need_head token
  if [ "${1:-}" = --rotate ]; then
    rm -f "$TOKEN_FILE"
    ensure_token
    if docker ps --format '{{.Names}}' | grep -qx rack-monitor; then
      bold "monitor: restarting with the new token"
      cmd_up >/dev/null
    fi
    dim "every app that had the old token needs the new one"
  fi
  [ -s "$TOKEN_FILE" ] || die "no token yet: rack monitor up"
  cat "$TOKEN_FILE"
}

cmd_logs() {
  if [ "${1:-}" = worker ]; then
    have_worker || die "worker unreachable"
    ssh -n "$WORKER_SSH" "docker logs --tail 40 rack-monitor 2>&1; echo; docker logs --tail 10 rack-monitor-docker 2>&1"
  else
    docker logs --tail 40 rack-monitor 2>&1; echo; docker logs --tail 10 rack-monitor-docker 2>&1
  fi
}

case "${1:-}" in
  up)     shift; cmd_up "$@" ;;
  down)   shift; cmd_down "$@" ;;
  status) shift; cmd_status "$@" ;;
  brief)  shift; cmd_brief "$@" ;;
  token)  shift; cmd_token "$@" ;;
  logs)   shift; cmd_logs "$@" ;;
  *) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
