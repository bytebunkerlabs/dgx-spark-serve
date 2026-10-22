#!/usr/bin/env bash
# rack gate: the rack's own front door (docs/14-gate.md).
#
#   rack gate up                 build the image, (re)start the container on the head
#   rack gate down               stop and remove it (keys and settings kept)
#   rack gate status             where it listens, what it routes, its clients
#   rack gate logs [-f]          its own log, then today's last requests
#   rack gate key add <client> [--models a,b] [--stdin] [--replace]
#   rack gate key ls | rm <client>
#
# One container, rack-gate: host network so it listens only on GATE_BIND,
# read-only, no capabilities, no-new-privileges, your uid, 256 MB. It mounts
# read-only the serving record, its settings and keys, and the engine key;
# read-write only its usage folder.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF=${DGX_SERVE_CONFIG:-$HOME/.config/dgx-serve}
STATE=${DGX_SERVE_STATE:-$HOME/.local/state/dgx-serve}
GATE_DIR=$CONF/gate
KEYS=$GATE_DIR/keys.json
SETTINGS=$GATE_DIR/gate.json
USAGE=$STATE/gate
ENGINE_KEY_FILE=${RACK_ENGINE_KEY_FILE:-$CONF/engine.key}
GATE_PORT=${GATE_PORT:-4001}
TAG=$(cat "$HERE/rackgate.py" "$HERE/Dockerfile" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)
IMAGE=rack-gate:$TAG
NAME=rack-gate

die() { echo "rack gate: $*" >&2; exit 1; }

# loopback and this machine's tailnet address unless GATE_BIND says otherwise
binds() {
  if [ -n "${GATE_BIND:-}" ]; then printf '%s' "$GATE_BIND"; return; fi
  local ts=""
  command -v tailscale >/dev/null 2>&1 && ts=$(tailscale ip -4 2>/dev/null | head -1 || true)
  printf '127.0.0.1%s' "${ts:+,$ts}"
}

ensure_files() {
  mkdir -p "$GATE_DIR" "$USAGE"
  chmod 700 "$GATE_DIR"
  [ -f "$KEYS" ] || { (umask 077; printf '{"clients": []}\n' > "$KEYS"); }
  [ -f "$SETTINGS" ] || printf '{"routes": [], "aliases": {}}\n' > "$SETTINGS"
  chmod 600 "$KEYS"
}

# keys.json edits: python3 (stdlib), written whole then renamed, 0600
keys_py() {
  KEYS="$KEYS" python3 - "$@" <<'PY'
import hashlib, json, os, secrets, sys, time
path, cmd, args = os.environ["KEYS"], sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {"clients": []}
rows = data.setdefault("clients", [])
def save():
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
if cmd == "add":
    name, models, from_stdin, replace = args[0], [m for m in args[1].split(",") if m] or ["*"], args[2] == "1", args[3] == "1"
    if any(r["name"] == name for r in rows) and not replace:
        sys.exit("rack gate: %s has a key already (--replace makes a new one)" % name)
    key = os.environ.get("KEY_IN", "").strip() if from_stdin else "rg-" + secrets.token_urlsafe(32)
    if len(key) < 16:
        sys.exit("rack gate: a key of at least 16 characters")
    rows[:] = [r for r in rows if r["name"] != name] + [{"name": name, "sha256": hashlib.sha256(key.encode()).hexdigest(),
                                                         "models": models, "created": time.strftime("%Y-%m-%d %H:%M")}]
    save()
    if not from_stdin:
        print(key)
elif cmd == "rm":
    before = len(rows)
    rows[:] = [r for r in rows if r["name"] != args[0]]
    if len(rows) == before:
        sys.exit("rack gate: no client named %s" % args[0])
    save()
elif cmd == "ls":
    for r in rows:
        print("  %-20s models %-30s since %s" % (r["name"], ",".join(r.get("models") or ["*"]), r.get("created", "?")))
    if not rows:
        print("  no clients yet: rack gate key add <client>")
PY
}

cmd_key() {
  local sub=${1:-ls}; shift || true
  ensure_files
  case "$sub" in
    add)
      local client="" models="" stdin=0 replace=0
      while [ $# -gt 0 ]; do
        case "$1" in
          --models) models=${2:-}; shift 2 ;;
          --stdin) stdin=1; shift ;;
          --replace) replace=1; shift ;;
          -*) die "unknown option $1" ;;
          *) client=$1; shift ;;
        esac
      done
      [[ "$client" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] || die "a client name: letters, digits, . _ - (up to 40)"
      if [ "$stdin" = 1 ]; then
        # read here: python's own stdin is its script. The key reaches it in the environment, never argv.
        local inkey; IFS= read -r inkey || true
        KEY_IN=$inkey keys_py add "$client" "$models" 1 "$replace"
        echo "rack gate: $client's key stored (as its sha256)" >&2
      else
        local key; key=$(keys_py add "$client" "$models" 0 "$replace")
        echo "rack gate: $client's key (shown once; the gate keeps only its sha256):" >&2
        printf '%s\n' "$key"
      fi ;;
    rm) [ -n "${1:-}" ] || die "usage: rack gate key rm <client>"; keys_py rm "$1"; echo "rack gate: $1's key removed" ;;
    ls) keys_py ls ;;
    *) die "usage: rack gate key add <client> [--models a,b] [--stdin] [--replace] | ls | rm <client>" ;;
  esac
}

cmd_up() {
  command -v docker >/dev/null 2>&1 || die "Docker is needed (rack gate runs as a container)"
  ensure_files
  [ -f "$ENGINE_KEY_FILE" ] || die "no engine key at $ENGINE_KEY_FILE: rack up makes one"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE"
    docker build -q -t "$IMAGE" "$HERE" >/dev/null
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  local b; b=$(binds)
  docker run -d --name "$NAME" --restart unless-stopped \
    --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --memory 256m --pids-limit 128 \
    -e "GATE_BIND=$b" -e "GATE_PORT=$GATE_PORT" \
    -e GATE_SERVING=/run/dgx-serve/serving.json -e GATE_CONFIG=/run/gate/gate.json \
    -e GATE_KEYS=/run/gate/keys.json -e GATE_ENGINE_KEY=/run/secrets/engine.key -e GATE_STATE=/run/gate-state \
    -v "$STATE":/run/dgx-serve:ro -v "$GATE_DIR":/run/gate:ro -v "$USAGE":/run/gate-state \
    -v "$ENGINE_KEY_FILE":/run/secrets/engine.key:ro \
    --label ai.bytebunker.rack-gate=gate "$IMAGE" serve >/dev/null
  local first=${b%%,*} i
  for i in $(seq 1 30); do
    if curl -fsS -m 2 "http://$first:$GATE_PORT/health" >/dev/null 2>&1; then
      echo "rack gate: listening on $(echo "$b" | sed "s/,/:$GATE_PORT, /g"):$GATE_PORT"
      echo "  OpenAI:    http://<address>:$GATE_PORT/v1    Anthropic: ANTHROPIC_BASE_URL=http://<address>:$GATE_PORT"
      echo "  a key per client: rack gate key add <client>"
      return 0
    fi
    sleep 1
  done
  docker logs --tail 20 "$NAME" >&2 || true
  die "it did not answer on $first:$GATE_PORT"
}

cmd_status() {
  local st; st=$(docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' "$NAME" 2>/dev/null || echo "not running")
  echo "container  $NAME: $st"
  local b; b=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" 2>/dev/null | sed -n 's/^GATE_BIND=//p')
  [ -n "$b" ] && echo "listens    $(echo "$b" | sed "s/,/:$GATE_PORT, /g"):$GATE_PORT"
  curl -fsS -m 2 "http://127.0.0.1:$GATE_PORT/health" >/dev/null 2>&1 && echo "health     answers" || echo "health     not answering on 127.0.0.1:$GATE_PORT"
  echo "routes"
  SERVING="$STATE/serving.json" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
def load(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None
s = load(os.environ["SERVING"])
if s and s.get("served_name"):
    print("  %-28s -> 127.0.0.1:%s (%s, serving here)" % (s.get("gateway_name") or s["served_name"], s.get("port"), s["served_name"]))
g = load(os.environ["SETTINGS"]) or {}
for r in g.get("routes") or []:
    print("  %-28s -> %s" % (r.get("name"), r.get("url")))
for a, t in (g.get("aliases") or {}).items():
    print("  %-28s => %s (alias)" % (a, t))
PY
  echo "clients"
  ensure_files
  keys_py ls
}

cmd_logs() {
  [ "${1:-}" = -f ] && exec docker logs -f "$NAME"
  docker logs "$NAME" 2>&1 | tail -20
  local f="$USAGE/usage-$(date -u +%Y-%m-%d).jsonl"     # named by UTC, as the gate names them
  [ -f "$f" ] && { echo "--- today's last requests"; tail -10 "$f"; }
  return 0
}

case "${1:-status}" in
  up) cmd_up ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 && echo "rack gate: stopped" || echo "rack gate: was not running" ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-}" ;;
  key) shift; cmd_key "$@" ;;
  -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "usage: rack gate up|down|status|logs|key" ;;
esac
