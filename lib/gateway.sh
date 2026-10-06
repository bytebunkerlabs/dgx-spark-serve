# shellcheck shell=bash
# lib/gateway.sh: rack gateway, for a site that fronts its engines with LiteLLM.
# Off unless GATEWAY_CONFIG names the LiteLLM config. Then rack up adds the
# serving recipe's route and rack down removes it, between dgx-serve's marker
# lines only (py/gateway.py), and LiteLLM is restarted to load it:
#   GATEWAY_CONFIG       the LiteLLM config file (on this machine)
#   GATEWAY_CONTAINER    its container, restarted after a change (else: say so)
#   GATEWAY_URL          where to check the route appeared (http://127.0.0.1:4000)
#   GATEWAY_KEY_FILE     LiteLLM's master key, raw or as LITELLM_MASTER_KEY=...
#   GATEWAY_ENGINE_HOST  the address LiteLLM reaches the engine at (the head's
#                        fabric address, else this machine's LAN address)
# A recipe may name its public route GATEWAY_NAME (model.env); else the served name.

gateway_on() { [ -n "${GATEWAY_CONFIG:-}" ]; }

gateway_engine_host() {
  [ -n "${GATEWAY_ENGINE_HOST:-}" ] && { printf '%s' "$GATEWAY_ENGINE_HOST"; return; }
  [ -n "${HEAD_IP:-}" ] && has_local_ip "$HEAD_IP" && { printf '%s' "$HEAD_IP"; return; }
  python3 -c 'import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try: s.connect(("1.1.1.1",80)); print(s.getsockname()[0])
except OSError: print("127.0.0.1")'
}

# Restart LiteLLM after a change, and wait until it lists (or stops listing) the route.
gateway_reload() { # gateway_reload <name> <present|absent>
  local name=$1 want=$2
  if [ -z "${GATEWAY_CONTAINER:-}" ]; then
    dim "  restart LiteLLM to load it (GATEWAY_CONTAINER names its container, and rack does it)"
    return 0
  fi
  docker restart "$GATEWAY_CONTAINER" >/dev/null || { warn "gateway: could not restart $GATEWAY_CONTAINER"; return 1; }
  [ -n "${GATEWAY_KEY_FILE:-}" ] || return 0
  GW_URL=${GATEWAY_URL:-http://127.0.0.1:4000} GW_KEY_FILE=$GATEWAY_KEY_FILE python3 - "$name" "$want" <<'PY'
import json, os, sys, time, urllib.request
name, want = sys.argv[1], sys.argv[2]
raw = open(os.environ["GW_KEY_FILE"]).read()
key = next((l.split("=", 1)[1].strip().strip('"') for l in raw.splitlines() if l.startswith("LITELLM_MASTER_KEY=")),
           raw.strip())
req = urllib.request.Request(os.environ["GW_URL"].rstrip("/") + "/v1/models", headers={"Authorization": "Bearer " + key})
for _ in range(30):
    time.sleep(2)
    try:
        ids = [m.get("id") for m in json.load(urllib.request.urlopen(req, timeout=5)).get("data", [])]
    except Exception:
        continue
    if (name in ids) == (want == "present"):
        print("  gateway: %s %s on %s" % (name, "live" if want == "present" else "gone", os.environ["GW_URL"]))
        sys.exit(0)
sys.stderr.write("gateway: LiteLLM restarted, but %s is %s: docker logs it\n" % (name, "missing" if want == "present" else "still listed"))
sys.exit(1)
PY
}

# Add or refresh the route to what rack up recorded as serving here.
gateway_sync() {
  local name served port key_ref out
  gateway_on || die "no gateway configured: GATEWAY_CONFIG names the LiteLLM config (docs/12-platforms.md)"
  served=$(serving_get served_name); port=$(serving_get port)
  [ -n "$served" ] || die "nothing is serving here (rack up first)"
  name=$(serving_get gateway_name); name=${name:-$served}
  key_ref=none
  [ "$(serving_get key_required)" = True ] && key_ref=os.environ/DGX_SERVE_ENGINE_KEY
  out=$(python3 "$ROOT/py/gateway.py" sync "$GATEWAY_CONFIG" "$name" "$served" \
        "http://$(gateway_engine_host):$port/v1" "$key_ref") || die "gateway: not changed"
  if [ "$out" = unchanged ]; then dim "  gateway: $name already routed"; return 0; fi
  bold "gateway: $name -> $served on :$port (between dgx-serve's markers in $GATEWAY_CONFIG)"
  [ "$key_ref" = none ] || dim "  LiteLLM needs the engine key in its environment as DGX_SERVE_ENGINE_KEY ($ENGINE_KEY_FILE)"
  gateway_reload "$name" present
}

gateway_remove() { # gateway_remove [<name>]  (default: the serving recipe's)
  local name=${1:-} out
  gateway_on || return 0
  [ -n "$name" ] || { name=$(serving_get gateway_name); [ -n "$name" ] || name=$(serving_get served_name); }
  [ -n "$name" ] || return 0
  out=$(python3 "$ROOT/py/gateway.py" remove "$GATEWAY_CONFIG" "$name") || return 1
  [ "$out" = changed ] || return 0
  bold "gateway: removed $name"
  gateway_reload "$name" absent
}

cmd_gateway() {
  local sub=${1:-status}
  [ $# -gt 0 ] && shift
  case "$sub" in
    status)
      gateway_on || { dim "no gateway: rack serves its engines directly (GATEWAY_CONFIG adds a LiteLLM route)"; return 0; }
      python3 "$ROOT/py/gateway.py" status "$GATEWAY_CONFIG" | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("  config     %s" % d["config"])
print("  rack keeps %s" % (", ".join(d["managed"]) or "nothing yet"))
print("  by hand    %s" % (", ".join(d["by_hand"]) or "nothing"))' ;;
    sync) gateway_sync ;;
    remove) [ $# -eq 1 ] || die "usage: rack gateway remove <name>"; gateway_remove "$1" ;;
    adopt)
      [ $# -eq 1 ] || die "usage: rack gateway adopt <name>"
      gateway_on || die "no gateway configured: GATEWAY_CONFIG"
      python3 "$ROOT/py/gateway.py" adopt "$GATEWAY_CONFIG" "$1" >/dev/null || die "gateway: not changed"
      bold "gateway: $1 is rack's now (rack down removes it with its recipe)" ;;
    *) die "usage: rack gateway [status|sync|remove <name>|adopt <name>]" ;;
  esac
}
