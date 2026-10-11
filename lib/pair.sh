# shellcheck shell=bash
# lib/pair.sh: pairing with the ByteBunker app, and the only command its key runs.
#
#   rack pair [--json] [--key '<ssh public key>'] [--name <app>]
#       What the app needs to reach this rack: this node, its addresses, the
#       engine (port, key, what it serves) and the monitor (port, token).
#       With --key, the app's own SSH key goes into ~/.ssh/authorized_keys,
#       restricted to `rack remote`: rack's own commands and nothing else (no
#       shell, no forwarding, no terminal). --json is for the app reading it
#       over ssh and carries the keys; a person gets a summary without them.
#   rack unpair [--name <app>]  remove that app's key (every app's without --name)
#   rack remote --app <name>    the forced command of an app's key: runs
#       SSH_ORIGINAL_COMMAND only when it is one of the commands below, and
#       `unpair`, which takes back that app's own key and no other
# Bash 3.2.

PAIR_TAG=bytebunker            # an app's key line ends with bytebunker:<name>
AUTH_KEYS=${RACK_AUTHORIZED_KEYS:-$HOME/.ssh/authorized_keys}
REMOTE_LOG=$DGX_SERVE_STATE/remote.log

realpath_of() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

# The rack an app's key runs: the installed command when it is this one (an
# update moves what the link points to, not the link), else this checkout's.
pair_rack_path() {
  local c me
  me=$(realpath_of "$ROOT/rack")
  for c in "$HOME/.local/bin/rack" "$(command -v rack 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -e "$c" ] || continue
    [ "$(realpath_of "$c")" = "$me" ] && { printf '%s' "$c"; return; }
  done
  printf '%s' "$ROOT/rack"
}

# One public key as ssh-keygen writes it: type, base64 body, maybe a comment.
# Options (command=..., from=...) are ours to write, never the caller's.
pair_key_ok() {
  case "$1" in *"
"*|*'"'*|*\\*) return 1 ;; esac
  printf '%s\n' "$1" | grep -Eq '^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) AAAA[A-Za-z0-9+/]+={0,3}( [^[:cntrl:]]*)?$'
}

pair_name_ok() { printf '%s' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'; }

# pair_install_key <key> <name>: add the app's key line, or replace the line
# that already holds the same key. Other lines are kept as they are.
pair_install_key() {
  local key=$1 name=$2 rp typ rest body
  rp=$(pair_rack_path)
  case "$rp" in
    *[!A-Za-z0-9._/+-]*) die "rack is at $rp: an authorized_keys line cannot carry that path (spaces or quotes). Install it where it can: ./rack install" ;;
  esac
  typ=${key%% *} rest=${key#* }
  body=${rest%% *}
  python3 - "$AUTH_KEYS" "$body" "command=\"$rp remote --app $name\",restrict $typ $body $PAIR_TAG:$name" <<'PY'
import os, sys, tempfile
path, body, line = sys.argv[1:4]
d = os.path.dirname(path)
os.makedirs(d, mode=0o700, exist_ok=True)
try:
    with open(path) as f:
        old = f.read().splitlines()
except FileNotFoundError:
    old = []
keep = [l for l in old if body not in l.split()]
replaced = len(keep) < len(old)
keep.append(line)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".authorized_keys.")
with os.fdopen(fd, "w") as f:
    f.write("\n".join(keep) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("replaced" if replaced else "added")
PY
}

# pair_remove_keys [name]: the lines rack pair wrote (that app's, or all).
pair_remove_keys() {
  python3 - "$AUTH_KEYS" "$PAIR_TAG" "${1:-}" <<'PY'
import os, sys, tempfile
path, tag, name = sys.argv[1:4]
try:
    with open(path) as f:
        old = f.read().splitlines()
except FileNotFoundError:
    print(0); sys.exit()
def ours(l):
    w = l.split()
    return l.startswith('command="') and ' remote' in l and '",restrict ' in l and bool(w) \
        and (w[-1] == "%s:%s" % (tag, name) if name else w[-1].startswith(tag + ":"))
keep = [l for l in old if not ours(l)]
if len(keep) != len(old):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".authorized_keys.")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
print(len(old) - len(keep))
PY
}

# The apps paired here, one name per line.
pair_apps() {
  [ -f "$AUTH_KEYS" ] || return 0
  awk -v t="$PAIR_TAG:" 'index($0, "command=\"") == 1 && index($0, " remote") && index($0, "\",restrict ") && index($NF, t) == 1 { print substr($NF, length(t) + 1) }' "$AUTH_KEYS"
}

# Everything the app needs, as one JSON document (secrets included: it goes
# to the app over ssh, and to nobody's terminal).
pair_json() {
  local ts="" dns="" port mport tok_file running answering name
  if have tailscale; then
    ts=$(tailscale ip -4 2>/dev/null | head -1 || true)
    dns=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))
except Exception: pass' 2>/dev/null || true)
  fi
  port=$(serving_get port); port=${port:-$API_PORT}
  mport=${MONITOR_PORT:-9177}
  tok_file=$HOME/.config/rack/monitor.token
  answering=0 running=0
  if have curl; then
    [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" 2>/dev/null)" = 200 ] && answering=1
    [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$mport/v1/hello" 2>/dev/null)" = 200 ] && running=1
  fi
  name=$( (inv_exists && inv_local_name) 2>/dev/null || true)
  PAIR_NAME=${name:-$HEAD_LABEL} PAIR_PLATFORM=$(this_platform) PAIR_TS=$ts PAIR_DNS=$dns \
  PAIR_PORT=$port PAIR_MPORT=$mport PAIR_KEY_FILE=$ENGINE_KEY_FILE PAIR_TOKEN_FILE=$tok_file \
  PAIR_SERVING=$DGX_SERVE_STATE/serving.json PAIR_ANSWERING=$answering PAIR_RUNNING=$running \
  PAIR_IS_HEAD=$IS_HEAD PAIR_VERSION=$RACK_VERSION PAIR_SCHEMA=$RACK_JSON_SCHEMA PAIR_APPS="$(pair_apps | tr '\n' ' ')" \
  python3 - <<'PY'
import json, os, socket
e = os.environ
def read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return ""
try:
    serving = json.load(open(e["PAIR_SERVING"]))
except (OSError, ValueError):
    serving = None
addrs, seen = [], set()
def add(ip, kind, name=None):
    if ip and ip not in seen:
        seen.add(ip)
        a = {"ip": ip, "kind": kind}
        if name:
            a["name"] = name
        addrs.append(a)
add(e.get("PAIR_TS"), "tailnet", e.get("PAIR_DNS") or None)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:                              # the address the default route leaves from; a UDP connect sends nothing
    s.connect(("1.1.1.1", 80))
    add(s.getsockname()[0], "lan")
except OSError:
    pass
finally:
    s.close()
print(json.dumps({
    "schema": int(e["PAIR_SCHEMA"]), "rack_version": e["PAIR_VERSION"],
    "name": e["PAIR_NAME"], "platform": e["PAIR_PLATFORM"], "head": e["PAIR_IS_HEAD"] == "1",
    "hostname": socket.gethostname(), "addresses": addrs,
    "engine": {"port": int(e["PAIR_PORT"]), "key": read(e["PAIR_KEY_FILE"]),
               "answering": e["PAIR_ANSWERING"] == "1", "serving": serving},
    "monitor": {"port": int(e["PAIR_MPORT"]), "token": read(e["PAIR_TOKEN_FILE"]),
                "running": e["PAIR_RUNNING"] == "1"},
    "apps": e.get("PAIR_APPS", "").split(),
}))
PY
}

cmd_pair() {
  local json=0 key="" name=app res
  while [ $# -gt 0 ]; do
    case "$1" in
      --json) json=1; shift ;;
      --key) [ $# -ge 2 ] || die "--key needs the app's public key"; key=$2; shift 2 ;;
      --name) [ $# -ge 2 ] || die "--name needs a name"; name=$2; shift 2 ;;
      *) die "usage: rack pair [--json] [--key '<ssh public key>'] [--name <app>]" ;;
    esac
  done
  pair_name_ok "$name" || die "an app's name is letters, digits, . _ - (up to 40): $name"
  [ "$IS_HEAD" = 1 ] || die "pair with the machine that serves: run rack pair on $HEAD_LABEL"
  if [ -n "$key" ]; then
    pair_key_ok "$key" || die "that is not one SSH public key (ssh-ed25519 AAAA... as ssh-keygen writes it)"
    res=$(pair_install_key "$key" "$name")
    [ "$json" = 1 ] || bold "$name's key $res: it can run rack commands here, and nothing else"
  fi
  if [ "$json" = 1 ]; then pair_json; return; fi
  pair_json | python3 -c '
import json, sys
d = json.load(sys.stdin)
eng, mon = d["engine"], d["monitor"]
s = eng.get("serving") or {}
print("rack pair: %s (%s)" % (d["name"], d["platform"]))
print("  addresses  %s" % (", ".join("%s (%s%s)" % (a["ip"], a["kind"], ", " + a["name"] if a.get("name") else "")
                                      for a in d["addresses"]) or "none found"))
print("  engine     :%d, %s%s" % (eng["port"], ("serving " + s["recipe"]) if s.get("recipe") else "nothing serving",
                                 ", key required" if eng["key"] else ", no key"))
print("  monitor    :%d, %s" % (mon["port"], "running" if mon["running"] else "not running (rack monitor up)"))
print("  apps       %s" % (", ".join(d["apps"]) or "none paired yet"))
print()
print("In ByteBunker: Add a rack, then this machine as user@host. The app asks over ssh; nothing to copy.")'
}

cmd_unpair() {
  local name="" n
  case "${1:-}" in
    --name) [ -n "${2:-}" ] || die "--name needs a name"; name=$2 ;;
    "") ;;
    *) die "usage: rack unpair [--name <app>]" ;;
  esac
  n=$(pair_remove_keys "$name")
  bold "removed $n app key$([ "$n" = 1 ] || echo s)${name:+ ($name)}"
}

# What an app's key may ask for: rack's own read and serve commands. Never a
# shell, never --on (another machine), never a change to who may log in.
remote_allowed() {
  local cmd=${1:-} sub=${2:-} w
  for w in "$@"; do case "$w" in --on|--on=*) return 1 ;; esac; done
  case "$cmd" in
    version|platform|status|preflight|models|net|bench|logs|up|down|pull|fit) return 0 ;;
    recipes) case "$sub" in ''|show|check|--*) return 0 ;; esac ;;
    nodes)   case "$sub" in ''|ls|test|--json) return 0 ;; esac ;;
    gateway) case "$sub" in ''|status|sync) return 0 ;; esac ;;
    monitor) case "$sub" in status|up) return 0 ;; esac ;;
    pair)    [ "$sub" = --json ] && [ $# -eq 2 ] && return 0 ;;
  esac
  return 1
}

REMOTE_APP=""
remote_log() {
  mkdir -p "$(dirname "$REMOTE_LOG")" 2>/dev/null || return 0
  if [ -f "$REMOTE_LOG" ] && [ "$(wc -c < "$REMOTE_LOG")" -gt 1048576 ]; then mv -f "$REMOTE_LOG" "$REMOTE_LOG.1"; fi
  printf '%s %s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${SSH_CLIENT%% *}" "${REMOTE_APP:-?}" "$1" "$2" \
    >> "$REMOTE_LOG" 2>/dev/null || true
}

cmd_remote() {
  local line=${SSH_ORIGINAL_COMMAND-} words
  case "${1:-}" in
    --app) pair_name_ok "${2:-}" || die "rack remote: --app needs the app's name"; REMOTE_APP=$2 ;;
    "") ;;
    *) die "usage: rack remote [--app <name>] (the forced command of an app's key)" ;;
  esac
  if [ -z "$line" ]; then
    die "rack remote: this key runs rack's commands only (ssh <this machine> rack status)"
  fi
  case "$line" in
    *[!A-Za-z0-9\ ._:/=@+,-]*) remote_log refused "$line"; die "rack remote: refused (rack's commands only, no shell): $line" ;;
  esac
  read -r -a words <<< "$line"
  [ "${words[0]:-}" = rack ] && words=(${words[@]+"${words[@]:1}"})
  if [ -n "$REMOTE_APP" ] && [ "${words[*]:-}" = unpair ]; then     # an app takes back its own key
    remote_log ran "$line"
    exec "$ROOT/rack" unpair --name "$REMOTE_APP"
  fi
  if [ ${#words[@]} -eq 0 ] || ! remote_allowed "${words[@]}"; then
    remote_log refused "$line"
    die "rack remote: not one of the commands an app may run here: $line"
  fi
  remote_log ran "$line"
  exec "$ROOT/rack" "${words[@]}"
}
