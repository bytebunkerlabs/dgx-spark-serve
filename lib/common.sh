# shellcheck shell=bash disable=SC2034  # the variables are for the scripts that source this
# lib/common.sh: helpers shared by rack and its scripts.
# Bash 3.2 compatible (macOS /bin/bash): no mapfile, no associative arrays,
# no ${var,,}. GNU or BSD userland. Source it; it defines functions and a few
# variables, and runs nothing.

RACK_VERSION=1.0.0-dev
RACK_RECIPE_SCHEMA=2
RACK_JSON_SCHEMA=1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Where dgx-serve keeps its own config and state: outside the checkout, so an
# update never touches them. Tests point these at temporary directories.
DGX_SERVE_CONFIG=${DGX_SERVE_CONFIG:-$HOME/.config/dgx-serve}
DGX_SERVE_STATE=${DGX_SERVE_STATE:-$HOME/.local/state/dgx-serve}

# JSON string body for $1 (quotes, backslashes and control characters).
json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}
json_str() { printf '"%s"' "$(json_escape "$1")"; }
json_num() { case "$1" in ''|*[!0-9.-]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
json_bool() { [ "$1" = 1 ] && printf 'true' || printf 'false'; }

# IPv4 addresses on this machine, from `ip` (Linux) or `ifconfig` (macOS).
local_ipv4s() {
  if have ip; then
    ip -o -4 addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif have ifconfig; then
    ifconfig 2>/dev/null | awk '/inet /{print $2}' | sed 's/^addr://'
  fi
}
has_local_ip() { [ -n "$1" ] && local_ipv4s | grep -qx "$1"; }

# sha256 of a file: sha256sum (GNU), shasum (macOS), or Python as a last resort.
sha256_of() {
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif have shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else python3 -c 'import hashlib,sys;h=hashlib.sha256();f=open(sys.argv[1],"rb")
for b in iter(lambda:f.read(1<<20),b""): h.update(b)
print(h.hexdigest())' "$1"
  fi
}

# Octal permission bits of a path (stat -c is GNU-only, stat -f BSD-only).
file_mode() { python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }

# A secret file written 0600 from the start, never world-readable for a moment.
write_secret() { # write_secret <path> <value>
  local path=$1 value=$2
  mkdir -p "$(dirname "$path")"
  ( umask 077; printf '%s\n' "$value" > "$path.tmp" ) && mv -f "$path.tmp" "$path"
}
new_secret() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }

# Where rack init keeps the engine's API key: the key itself, and the same key
# as an env file for `docker run --env-file`. Both 0600, never printed.
ENGINE_KEY_FILE=$DGX_SERVE_CONFIG/engine.key
ENGINE_ENV_FILE=$DGX_SERVE_CONFIG/engine.env
GATEWAY_ENV_FILE=$DGX_SERVE_CONFIG/gateway.env

# The engine key's other forms, beside it and 0600 like it: what the engine
# reads (VLLM_API_KEY) and what a LiteLLM gateway reads (DGX_SERVE_ENGINE_KEY,
# through env_file in its compose service, so no config you commit holds it).
# Written when missing, and again for a new key (engine_key_forms new).
engine_key_forms() {
  if [ ! -s "$ENGINE_ENV_FILE" ] || [ "${1:-}" = new ]; then
    write_secret "$ENGINE_ENV_FILE" "VLLM_API_KEY=$(cat "$ENGINE_KEY_FILE")"
  fi
  if [ ! -s "$GATEWAY_ENV_FILE" ] || [ "${1:-}" = new ]; then
    write_secret "$GATEWAY_ENV_FILE" "DGX_SERVE_ENGINE_KEY=$(cat "$ENGINE_KEY_FILE")"
  fi
}

# json_get <key> < document: a top-level value as text (true/false for
# booleans, the length of a list, empty for null or missing).
json_get() {
  python3 -c 'import json,sys
d=json.loads(sys.stdin.read() or "{}")
v=d.get(sys.argv[1])
if isinstance(v,bool): print("true" if v else "false")
elif isinstance(v,list): print(len(v))
elif v is not None: print(v)' "$1"
}

# The interface that owns an IPv4 address (Linux).
iface_of_ip() { have ip && ip -o -4 addr show 2>/dev/null | awk -v a="$1" 'index($4, a "/") == 1 {print $2; exit}'; }

# One ping with a two-second limit: -W is seconds on Linux, -t on macOS.
ping_once() {
  if [ "$(uname -s 2>/dev/null)" = Darwin ]; then ping -c 1 -t 2 "$1"; else ping -c 1 -W 2 "$1"; fi >/dev/null 2>&1
}

# Free space in GB where a path is, or would be created (df -P is POSIX).
disk_free_gb() {
  local p=$1
  while [ ! -e "$p" ] && [ "$p" != / ] && [ -n "$p" ]; do p=$(dirname "$p"); done
  df -Pk "${p:-/}" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}'
}

# A name rack can use for a machine: what follows any user@, with anything
# that is not a letter, digit, dot, dash or underscore turned into a dash.
sanitize_name() {
  printf '%s' "${1##*@}" | tr -c 'A-Za-z0-9._-' '-' | sed 's/^[^A-Za-z0-9]*//' | cut -c1-63
}

# Site settings (HF_CACHE, IMAGE, API_PORT, TOPOLOGY...): $DGX_SERVE_CONFIG/rack.env,
# then the checkout's .env from before 1.0, which therefore still wins.
# DGX_SERVE_DOTENV points the second somewhere else (tests do).
load_site_env() {
  # shellcheck source=/dev/null
  [ -f "$DGX_SERVE_CONFIG/rack.env" ] && . "$DGX_SERVE_CONFIG/rack.env"
  # shellcheck source=/dev/null
  [ -f "${DGX_SERVE_DOTENV:-${RACK_ROOT:-$PWD}/.env}" ] && . "${DGX_SERVE_DOTENV:-${RACK_ROOT:-$PWD}/.env}"
  return 0
}
