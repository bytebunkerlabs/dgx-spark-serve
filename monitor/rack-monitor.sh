#!/usr/bin/env bash
# rack monitor: one telemetry endpoint for the whole rack.
#
#   rack monitor up [--bare]     build, ship to the workers, (re)start on every node
#   rack monitor down            stop and remove the monitor (token kept)
#   rack monitor status          containers or service, health, and the endpoint to add
#   rack monitor token [--rotate]  print the token, or replace it and restart
#   rack monitor logs [<worker>] the monitor's own log
#
# Run it on the head. Where Docker serves (DGX Spark, NVIDIA Linux) every node
# gets two containers from one small image:
#
#   rack-monitor         host network and host pids so it can see the node,
#                        but read-only, no capabilities, your uid, 256 MB.
#                        Samples every 2 s, serves :9177 behind one token.
#   rack-monitor-docker  no network at all. The only thing holding the Docker
#                        socket; it writes the container list to a file the
#                        monitor reads. The network-facing process never
#                        talks to Docker.
#
# The head's monitor also asks the workers' over the fabric, so the app needs
# one endpoint and one token for the whole rack.
#
# Bare (a Mac, Windows through WSL2, --bare anywhere): no image, no Docker.
# rackmon.py runs as your user under launchd or a systemd user unit, reads
# the machine the same read-only way, and restarts with it.  docs/11-monitor.md
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
HEAD_IP=${HEAD_IP:-192.168.100.1}
WORKER_IP=${WORKER_IP:-192.168.100.2}
HEAD_LABEL=${HEAD_LABEL:-${HEAD_SSH:-spark-1}}
WORKER_SSH=${WORKER_SSH-spark-2}             # empty: a single-node site
# rack passes its inventory's workers, one per line: name<TAB>ssh<TAB>fabric ip.
# Run directly, the .env's single worker stands in.
if [ -n "${RACK_MONITOR_WORKERS+set}" ]; then WORKERS_TSV=$RACK_MONITOR_WORKERS
elif [ -n "$WORKER_SSH" ]; then WORKERS_TSV=$(printf '%s\t%s\t%s' "$WORKER_SSH" "$WORKER_SSH" "$WORKER_IP")
else WORKERS_TSV=""; fi
MONITOR_PORT=${MONITOR_PORT:-9177}
MONITOR_CLUSTER=${MONITOR_CLUSTER:-rack}
MONITOR_ENGINE_PORTS=${MONITOR_ENGINE_PORTS:-}
MONITOR_BIND=${MONITOR_BIND:-0.0.0.0}        # the head's listen addresses; the worker binds its fabric IP
TOKEN_FILE=$HOME/.config/rack/monitor.token  # same path on every node
STATE_DIR=$HOME/.local/state/rack-monitor
TAG=$(cat "$HERE/rackmon.py" "$HERE/Dockerfile" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)
IMAGE=rack-monitor:$TAG
MON_LABEL=ai.bytebunker.dgx-serve.monitor     # launchd job (bare, Mac)
MON_UNIT=rack-monitor.service                  # systemd user units (bare, Linux)
RELAY_UNIT=rack-monitor-relay.service
PLIST=$HOME/Library/LaunchAgents/$MON_LABEL.plist
UNIT_DIR=$HOME/.config/systemd/user

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# rack passes RACK_IS_HEAD (its inventory knows); run directly, owning the
# head's fabric IP is the test.
on_head() {
  [ -n "${RACK_IS_HEAD:-}" ] && { [ "$RACK_IS_HEAD" = 1 ]; return; }
  { ip -o addr show 2>/dev/null || true; } | grep -q " $HEAD_IP/"
}
reachable() { ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$1" true 2>/dev/null; }
workers() { printf '%s\n' "$WORKERS_TSV" | sed '/^$/d'; }     # name<TAB>ssh<TAB>fabric ip
need_head() {
  on_head || die "rack monitor runs on the head ($HEAD_LABEL): ssh $HEAD_LABEL, then rack monitor ${1:-up}"
  bare && return 0
  command -v docker >/dev/null || die "docker not found on $(hostname)"
}
# Bare when asked, when the bare service is what runs here, or where Docker cannot
# run the monitor: a Mac, or a machine without a usable Docker.
bare() {
  [ "${BARE:-0}" = 1 ] && return 0
  [ -f "$PLIST" ] || [ -f "$UNIT_DIR/$MON_UNIT" ] && return 0
  [ "$(uname -s)" = Darwin ] && return 0
  docker info >/dev/null 2>&1 && return 1
  return 0
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

ship_worker() {   # ship_worker <ssh-target>
  local remote
  remote=$(ssh -n "$1" "docker image inspect --format '{{.Id}}' '$IMAGE' 2>/dev/null" || true)
  if [ -z "$remote" ]; then
    bold "monitor: shipping $IMAGE to $1"
    docker save "$IMAGE" | ssh "$1" "docker load -q" >/dev/null
    ssh -n "$1" "docker tag '$IMAGE' rack-monitor:latest"
  fi
  # same token on every node: the head presents it when it asks the worker
  ssh "$1" "umask 077; mkdir -p ~/.config/rack && cat > ~/.config/rack/monitor.token" < "$TOKEN_FILE"
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
serving=$HOME/.local/state/dgx-serve
mkdir -p "$serving"
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
  -e "MONITOR_PORT=$port" -e "MONITOR_BIND=$bind" -e "MONITOR_CLUSTER=$cluster" ${extra[@]+"${extra[@]}"} \
  -v "$tok":/run/secrets/rack-monitor-token:ro \
  -v "$state":/run/rackmon:ro \
  -v "$serving":/run/dgx-serve:ro -e MONITOR_SERVING=/run/dgx-serve/serving.json \
  -v /etc/os-release:/run/host/os-release:ro \
  --label ai.bytebunker.rack-monitor=monitor \
  "$image" serve >/dev/null
}
if ! run_monitor ${gpu[@]+"${gpu[@]}"} 2>/dev/null; then
  docker rm -f rack-monitor >/dev/null 2>&1 || true
  run_monitor
  echo "started (without the GPU: docker refused --gpus all)"
  exit 0
fi
echo "started${gpu[0]:+ with the GPU}"
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
  local name target ip
  bash -c "$prune"
  while IFS='	' read -r name target ip; do
    reachable "$target" && ssh -n "$target" "$prune" || true
  done <<EOF
$(workers)
EOF
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
  local ts lan dns cidr dev code first
  ts=$(tailscale ip -4 2>/dev/null | head -1 || true)
  dns=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))
except Exception: pass' 2>/dev/null || true)
  dev=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1 || true)
  # the address the default route leaves from (a UDP connect sends nothing)
  lan=$(python3 -c 'import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try: s.connect(("1.1.1.1",80)); print(s.getsockname()[0])
except OSError: pass' 2>/dev/null || true)
  first=$(workers | head -1 | cut -f2)
  echo
  bold "add this to ByteBunker: Cluster > Add monitor"
  [ -n "$ts" ]  && printf '  tailnet   http://%s:%s\n' "$ts" "$MONITOR_PORT"
  [ -n "$dns" ] && printf '            http://%s:%s\n' "$dns" "$MONITOR_PORT"
  if [ -n "$lan" ]; then
    printf '  LAN       http://%s:%s' "$lan" "$MONITOR_PORT"
    # Measure, from the worker, whether the LAN path is open. ufw drops it by
    # default; Docker-published ports (LiteLLM's) bypass ufw, host-network ones don't.
    code=""
    if [ -n "$first" ] && reachable "$first"; then
      code=$(ssh -n "$first" "curl -s -m 3 -o /dev/null -w '%{http_code}' http://$lan:$MONITOR_PORT/v1/hello" 2>/dev/null || true)
    fi
    if [ "$code" = 200 ]; then
      echo "   (open)"
    elif [ -n "$code" ] && [ -n "$dev" ]; then
      cidr=$(ip -o -4 addr show dev "$dev" 2>/dev/null | awk '{print $4}' | head -1)
      echo "   (closed by the firewall; to open it on the LAN only:"
      echo "             sudo ufw allow from $(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$cidr") to any port $MONITOR_PORT proto tcp)"
    else
      echo "   (not measured: no worker to test from)"
    fi
  fi
  printf '  token     rack monitor token\n'
}

# ------------------------------------------------------------------ bare ----
# The monitor as a user service, from this checkout: launchd on a Mac, a
# systemd user unit elsewhere (and a relay unit when Docker is usable, so its
# containers still show). Same token, same port, same read-only sampling.
bare_env() {   # the monitor's environment, KEY=VALUE per line
  printf '%s\n' "MONITOR_NAME=$HEAD_LABEL" "MONITOR_ROLE=head" "MONITOR_PEERS=$1" "MONITOR_PORT=$MONITOR_PORT" \
    "MONITOR_BIND=$MONITOR_BIND" "MONITOR_CLUSTER=$MONITOR_CLUSTER" "MONITOR_TOKEN_FILE=$TOKEN_FILE" \
    "MONITOR_STATE_DIR=$STATE_DIR" "MONITOR_HOST_OS_RELEASE=/etc/os-release" \
    "MONITOR_SERVING=${RACK_SERVING:-$HOME/.local/state/dgx-serve/serving.json}"
  # the same user as the engine: it may read the engine key for its metrics
  [ -z "${RACK_ENGINE_KEY_FILE:-}" ] || printf 'MONITOR_ENGINE_KEY_FILE=%s\n' "$RACK_ENGINE_KEY_FILE"
  [ -z "$MONITOR_ENGINE_PORTS" ] || printf 'MONITOR_ENGINE_PORTS=%s\n' "$MONITOR_ENGINE_PORTS"
}

bare_up() {   # bare_up <peers>
  local py env_lines
  py=$(command -v python3) || die "python3 not found"
  mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
  env_lines=$(bare_env "$1")
  if [ "$(uname -s)" = Darwin ]; then
    mkdir -p "$(dirname "$PLIST")"
    ENV_LINES=$env_lines python3 - "$PLIST" "$MON_LABEL" "$py" "$HERE/rackmon.py" "$STATE_DIR/monitor.log" <<'PY'
import os, plistlib, sys
plist, label, py, script, log = sys.argv[1:6]
env = dict(l.split("=", 1) for l in os.environ["ENV_LINES"].splitlines() if "=" in l)
with open(plist, "wb") as f:
    plistlib.dump({"Label": label, "ProgramArguments": [py, script, "serve"], "EnvironmentVariables": env,
                   "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ProcessType": "Background",
                   "StandardOutPath": log, "StandardErrorPath": log}, f)
PY
    launchctl bootout "gui/$(id -u)/$MON_LABEL" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$MON_LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
    launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl bootstrap failed: rack monitor logs"
    printf 'monitor: %-10s started (launchd, %s)\n' "$HEAD_LABEL" "$MON_LABEL"
  else
    command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
      || die "bare mode needs systemd (WSL2: [boot] systemd=true in /etc/wsl.conf, then wsl --shutdown)"
    mkdir -p "$UNIT_DIR"
    {
      printf '[Unit]\nDescription=rack monitor: telemetry for this machine\nAfter=network-online.target\n\n[Service]\n'
      printf 'ExecStart=%s %s serve\n' "$py" "$HERE/rackmon.py"
      printf '%s\n' "$env_lines" | sed 's/^/Environment=/'
      printf 'Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n'
    } > "$UNIT_DIR/$MON_UNIT"
    if docker info >/dev/null 2>&1; then
      {
        printf '[Unit]\nDescription=rack monitor: the container list for the monitor\n\n[Service]\n'
        printf 'ExecStart=%s %s docker-relay\nEnvironment=MONITOR_STATE_DIR=%s\n' "$py" "$HERE/rackmon.py" "$STATE_DIR"
        printf 'Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n'
      } > "$UNIT_DIR/$RELAY_UNIT"
    fi
    systemctl --user daemon-reload
    systemctl --user enable "$MON_UNIT" >/dev/null 2>&1
    systemctl --user restart "$MON_UNIT"
    if [ -f "$UNIT_DIR/$RELAY_UNIT" ]; then
      systemctl --user enable "$RELAY_UNIT" >/dev/null 2>&1; systemctl --user restart "$RELAY_UNIT"
    fi
    printf 'monitor: %-10s started (systemd user unit %s)\n' "$HEAD_LABEL" "$MON_UNIT"
    [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" = yes ] \
      || dim "  it stops when you log out: sudo loginctl enable-linger $(id -un)"
  fi
}

bare_down() {
  if [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$MON_LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
  fi
  if [ -f "$UNIT_DIR/$MON_UNIT" ] || [ -f "$UNIT_DIR/$RELAY_UNIT" ]; then
    systemctl --user disable --now "$MON_UNIT" "$RELAY_UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$MON_UNIT" "$UNIT_DIR/$RELAY_UNIT"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
}

bare_running() {
  if [ "$(uname -s)" = Darwin ]; then launchctl print "gui/$(id -u)/$MON_LABEL" 2>/dev/null | grep -q 'state = running'
  else systemctl --user is-active --quiet "$MON_UNIT" 2>/dev/null; fi
}

cmd_up() {
  [ "${1:-}" = --bare ] && BARE=1
  need_head up
  ensure_token
  local peers="" name target ip
  # every worker's monitor answers the head over the fabric
  while IFS='	' read -r name target ip; do
    [ -n "$name" ] || continue
    peers="${peers:+$peers,}$name=http://${ip:-$target}:$MONITOR_PORT"
  done <<EOF
$(workers)
EOF
  if bare; then
    [ -z "$(workers)" ] || dim "monitor: bare mode runs here only; start each worker's own with rack monitor up --bare there"
    bare_up "$peers"
  else
    build_image
    while IFS='	' read -r name target ip; do
      [ -n "$name" ] || continue
      if reachable "$target"; then
        ship_worker "$target"
        # the worker answers only the head, over the fabric (and itself)
        printf 'monitor: %-10s ' "$name"; start_node "$target" "$name" worker "" "${ip:+$ip,}127.0.0.1"
      else
        dim "monitor: $name unreachable over ssh; the head will report it down until rack monitor up runs again"
      fi
    done <<EOF
$(workers)
EOF
    printf 'monitor: %-10s ' "$HEAD_LABEL"; start_node local "$HEAD_LABEL" head "$peers" "${MONITOR_BIND:-0.0.0.0}"
    prune_images
  fi
  local _
  for _ in $(seq 1 $(( ${MONITOR_WAIT_S:-30} / 2 ))); do    # the first samples take a moment
    sleep 2
    if summary >/dev/null 2>&1; then break; fi
  done
  bold "monitor: first samples"
  summary || dim "  (a node is still starting or unreachable: rack monitor status in a minute)"
  endpoints
}

cmd_down() {
  need_head down
  local name target ip
  if bare; then
    bare_down
    echo "monitor: $HEAD_LABEL stopped"
  else
    docker rm -f rack-monitor rack-monitor-docker >/dev/null 2>&1 || true
    echo "monitor: $HEAD_LABEL stopped"
    while IFS='	' read -r name target ip; do
      [ -n "$name" ] || continue
      if reachable "$target"; then
        ssh -n "$target" "docker rm -f rack-monitor rack-monitor-docker >/dev/null 2>&1 || true"
        echo "monitor: $name stopped"
      fi
    done <<EOF
$(workers)
EOF
  fi
  dim "token kept in $TOKEN_FILE; rack monitor up brings it back with the same one"
}

cmd_status() {
  need_head status
  local name target ip
  if bare; then
    bold "service"
    if bare_running; then printf '  %s  running (%s)\n' "$HEAD_LABEL" "$([ -f "$PLIST" ] && echo "launchd $MON_LABEL" || echo "systemd $MON_UNIT")"
    else dim "  $HEAD_LABEL  not running: rack monitor up"; fi
  else
    bold "containers"
    docker ps -a --filter label=ai.bytebunker.rack-monitor --format '  {{.Names}}\t{{.Status}}\t{{.Image}}' | sed "s/^/  $HEAD_LABEL/" || true
    while IFS='	' read -r name target ip; do
      [ -n "$name" ] || continue
      reachable "$target" || { dim "  $name  unreachable over ssh"; continue; }
      ssh -n "$target" "docker ps -a --filter label=ai.bytebunker.rack-monitor --format '  {{.Names}}\t{{.Status}}\t{{.Image}}'" | sed "s/^/  $name/" || true
    done <<EOF
$(workers)
EOF
  fi
  echo; bold "nodes"
  summary || true
  endpoints
}

cmd_brief() {   # one line for `rack status`
  on_head || { dim "  (run on the head to see the monitor)"; return 0; }
  if bare; then
    bare_running || { dim "  not running: rack monitor up"; return 0; }
  elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rack-monitor; then
    dim "  not running: rack monitor up"; return 0
  fi
  summary 2>/dev/null || dim "  running, not answering yet: rack monitor status"
}

cmd_token() {
  need_head token
  if [ "${1:-}" = --rotate ]; then
    rm -f "$TOKEN_FILE"
    ensure_token
    if { bare && bare_running; } || docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rack-monitor; then
      bold "monitor: restarting with the new token"
      cmd_up >/dev/null
    fi
    dim "every app that had the old token needs the new one"
  fi
  [ -s "$TOKEN_FILE" ] || die "no token yet: rack monitor up"
  cat "$TOKEN_FILE"
}

cmd_logs() {
  local target
  if [ -n "${1:-}" ]; then
    target=$(workers | awk -F'\t' -v w="$1" '$1 == w || w == "worker" {print $2; exit}')
    [ -n "$target" ] || die "no such worker: $1"
    reachable "$target" || die "$1 is unreachable over ssh"
    ssh -n "$target" "docker logs --tail 40 rack-monitor 2>&1; echo; docker logs --tail 10 rack-monitor-docker 2>&1"
  elif bare; then
    if [ -f "$STATE_DIR/monitor.log" ]; then tail -n 40 "$STATE_DIR/monitor.log"
    else journalctl --user -u "$MON_UNIT" -n 40 --no-pager 2>/dev/null || dim "no log yet"; fi
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
