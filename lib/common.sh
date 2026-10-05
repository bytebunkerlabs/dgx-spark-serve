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
