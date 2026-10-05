# shellcheck shell=bash disable=SC2034  # the site variables are for the scripts that source this
# lib/inventory.sh: the machines in this rack, one file per node.
#
#   $DGX_SERVE_CONFIG/nodes/<name>.env     KEY=VALUE lines, written by rack
#     NODE_NAME       the node's name, in rack and in the app
#     NODE_SSH        how this machine reaches it (an ssh alias or user@host);
#                     empty on this machine's own entry
#     NODE_PLATFORM   dgx, linux, windows or mac (what `rack platform` says there)
#     NODE_ROLE       head    serves, and fronts its workers
#                     worker  a head's peer in one model across machines
#                     node    serves its own model; reached with --on <name>
#     NODE_LOCAL      1 on this machine's own entry
#     NODE_FABRIC_IP NODE_FABRIC_IF NODE_IB_HCAS   the network NCCL uses
#     NODE_GPUS       how many GPUs
#     NODE_RACK_DIR   where rack is installed there, relative to that $HOME
#     NODE_HF_CACHE   the Hugging Face cache there (default: same path as here)
#
# It replaces the fixed head/worker pair of .env (HEAD_IP, WORKER_SSH,
# WORKER_IP), which `rack init` imports. Node files are data: parsed line by
# line, never sourced, and rack refuses to write a value a shell could
# interpret. Bash 3.2.

INV_KEYS="NODE_NAME NODE_SSH NODE_PLATFORM NODE_ROLE NODE_LOCAL NODE_FABRIC_IP NODE_FABRIC_IF NODE_IB_HCAS NODE_GPUS NODE_RACK_DIR NODE_HF_CACHE"
# The author's two-Spark rack, where every pre-inventory default came from.
LEGACY_HEAD_IP=192.168.100.1 LEGACY_WORKER_IP=192.168.100.2
LEGACY_HEAD=spark-1 LEGACY_WORKER=spark-2

inv_dir() { printf '%s/nodes' "$DGX_SERVE_CONFIG"; }
inv_valid_name() { printf '%s' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'; }
inv_file() { printf '%s/%s.env' "$(inv_dir)" "$1"; }
inv_exists() { ls "$(inv_dir)"/*.env >/dev/null 2>&1; }
inv_has() { [ -f "$(inv_file "$1")" ]; }

inv_get() { # inv_get <name> <KEY>
  local f; f=$(inv_file "$1")
  [ -f "$f" ] || return 0
  sed -n "s/^$2=//p" "$f" | head -1
}

inv_names() { # head first, then the rest by name
  local f n heads="" rest=""
  for f in "$(inv_dir)"/*.env; do
    [ -f "$f" ] || continue
    n=$(basename "$f" .env)
    if [ "$(inv_get "$n" NODE_ROLE)" = head ]; then heads="$heads $n"; else rest="$rest $n"; fi
  done
  for n in $heads $rest; do printf '%s\n' "$n"; done
}

# A value rack will write: no whitespace, quotes, $, backticks or backslashes,
# so a node file can never smuggle a command into the ssh lines built from it.
inv_safe_value() {
  case "$1" in *[[:space:]]*|*\"*|*\'*|*'$'*|*'`'*|*\\*|*';'*|*'|'*|*'&'*|*'<'*|*'>'*) return 1 ;; esac
  return 0
}

inv_set() { # inv_set <name> KEY=VALUE...   (creates or updates, atomically)
  local name=$1 f tmp kv k v key; shift
  inv_valid_name "$name" || die "node names are letters, digits, dot, dash and underscore: '$name'"
  for kv in "$@"; do
    case " $INV_KEYS " in *" ${kv%%=*} "*) ;; *) die "unknown node key: ${kv%%=*}" ;; esac
    inv_safe_value "${kv#*=}" || die "refusing ${kv%%=*}='${kv#*=}': no spaces, quotes or shell characters"
  done
  f=$(inv_file "$name")
  mkdir -p "$(inv_dir)"
  tmp="$f.tmp.$$"
  : > "$tmp"
  for k in $INV_KEYS; do
    v=$(inv_get "$name" "$k")
    [ "$k" = NODE_NAME ] && v=$name
    for kv in "$@"; do
      key=${kv%%=*}
      [ "$key" = "$k" ] && v=${kv#*=}
    done
    printf '%s=%s\n' "$k" "$v" >> "$tmp"
  done
  mv -f "$tmp" "$f"
}

inv_rm() { rm -f "$(inv_file "$1")"; }

inv_local_name() { # this machine's entry, if any
  local n
  for n in $(inv_names); do [ "$(inv_get "$n" NODE_LOCAL)" = 1 ] && { printf '%s' "$n"; return; }; done
  return 0
}
inv_head_name() {
  local n
  for n in $(inv_names); do [ "$(inv_get "$n" NODE_ROLE)" = head ] && { printf '%s' "$n"; return; }; done
  return 0
}
inv_workers() {  # names of the head's workers, in name order
  local n
  for n in $(inv_names); do [ "$(inv_get "$n" NODE_ROLE)" = worker ] && printf '%s\n' "$n"; done
  return 0
}

# How to reach a node: its inventory ssh target, else the name itself (a
# pre-inventory WORKER_SSH is both).
node_ssh() { local s; s=$(inv_get "$1" NODE_SSH); printf '%s' "${s:-$1}"; }

# Where rack lives on a node, relative to that node's $HOME: the inventory's
# answer, else where it lives here (the same layout on every machine).
node_rack_dir() {
  local d; d=$(inv_get "$1" NODE_RACK_DIR)
  [ -n "$d" ] || { d=${RACK_ROOT:-$PWD}; d=${d#"$HOME"/}; }
  printf '%s' "$d"
}

# The node's Hugging Face cache; by default the same path as this machine's.
node_hf_cache() { local d; d=$(inv_get "$1" NODE_HF_CACHE); printf '%s' "${d:-$HF_CACHE}"; }

# Can we ssh to a node without a password? Asked once per run per node.
NODE_UP_CACHE=" "
node_up() {
  local n=$1 t
  case "$NODE_UP_CACHE" in *" $n=1 "*) return 0 ;; *" $n=0 "*) return 1 ;; esac
  t=$(node_ssh "$n")
  if [ -n "$t" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 "$t" true </dev/null >/dev/null 2>&1; then
    NODE_UP_CACHE="$NODE_UP_CACHE$n=1 "; return 0
  fi
  NODE_UP_CACHE="$NODE_UP_CACHE$n=0 "; return 1
}

# This machine's label in messages: its inventory name; on the author's rack
# before rack init, spark-1 or spark-2 (whatever the hostnames say); else its
# short hostname.
local_label() {
  local n; n=$(inv_local_name)
  [ -n "$n" ] && { printf '%s' "$n"; return; }
  if legacy_rack; then
    if has_local_ip "$LEGACY_HEAD_IP"; then printf '%s' "$LEGACY_HEAD"; else printf '%s' "$LEGACY_WORKER"; fi
    return
  fi
  n=$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)
  printf '%s' "${n:-this-node}"
}

# Is this the author's rack (a DGX Spark on the 192.168.100.0/24 fabric),
# where an unconfigured rack has always meant spark-1 and spark-2?
legacy_rack() {
  case "$(_plat_file /sys/class/dmi/id/product_name) $(_plat_file /sys/class/dmi/id/product_family)" in
    *DGX_Spark*|*"DGX Spark"*) ;;
    *) return 1 ;;
  esac
  has_local_ip "$LEGACY_HEAD_IP" || has_local_ip "$LEGACY_WORKER_IP"
}

is_localhost() {
  case "$1" in
    ''|localhost|127.0.0.1|::1) return 0 ;;
    "$(hostname -s 2>/dev/null || true)"|"$(hostname 2>/dev/null || true)") return 0 ;;
  esac
  return 1
}

# -------------------------------------------------------- site resolution --
# Workers of this rack. Sets WORKER_NAMES (inventory names, or the ssh target
# without an inventory), WORKERS (their ssh targets), and WORKER_SSH and
# WORKER_IP for the first. Before the inventory, an unset WORKER_SSH meant
# spark-2 on every machine: a phantom worker in status, a DOWN peer in the
# monitor, a failed image sync. Now:
#   inventory present         its workers
#   WORKER_SSH set, even ""   exactly that ("" is a single-node site)
#   the author's rack         spark-2
#   otherwise                 no worker
rack_resolve_workers() {
  local n
  WORKER_NAMES="" WORKERS=""
  if inv_exists; then
    n=$(inv_local_name)
    # a standalone node serves on its own: the rack's workers are not its
    if [ -z "$n" ] || [ "$(inv_get "$n" NODE_ROLE)" != node ]; then
      for n in $(inv_workers); do
        WORKER_NAMES="${WORKER_NAMES:+$WORKER_NAMES }$n"
        WORKERS="${WORKERS:+$WORKERS }$(node_ssh "$n")"
      done
    fi
    n=${WORKER_NAMES%% *}
    WORKER_SSH=""
    [ -n "$n" ] && { WORKER_SSH=$(node_ssh "$n"); WORKER_IP=$(inv_get "$n" NODE_FABRIC_IP); }
  elif [ -n "${WORKER_SSH+set}" ]; then
    WORKER_NAMES=$WORKER_SSH WORKERS=$WORKER_SSH
  elif legacy_rack; then
    WORKER_SSH=$LEGACY_WORKER WORKER_NAMES=$LEGACY_WORKER WORKERS=$LEGACY_WORKER
    WORKER_IP=${WORKER_IP:-$LEGACY_WORKER_IP}
  else
    WORKER_SSH=""
  fi
  return 0
}

# Is this machine the head? With an inventory, its own entry says (a worker
# is not; no entry at all is a laptop driving a rack elsewhere). Without one:
# the machine that owns HEAD_IP is, the one that owns WORKER_IP is not, a
# HEAD_SSH naming another machine means a laptop, and otherwise the machine
# you type on is its own head. No fabric IP needed: a cloud box or a WSL2 PC
# is a head.
rack_is_head() {
  local me
  if inv_exists; then
    me=$(inv_local_name)
    [ -n "$me" ] || return 1
    [ "$(inv_get "$me" NODE_ROLE)" = worker ] && return 1
    return 0
  fi
  [ -n "${HEAD_IP:-}" ] && has_local_ip "$HEAD_IP" && return 0
  [ -n "${WORKER_IP:-}" ] && has_local_ip "$WORKER_IP" && return 1
  if legacy_rack; then has_local_ip "$LEGACY_HEAD_IP"; return; fi
  [ -n "${HEAD_SSH:-}" ] && ! is_localhost "$HEAD_SSH" && return 1
  return 0
}

# Everything rack needs to know about the site, after .env: the workers, the
# head (HEAD_IP, HEAD_SSH, HEAD_LABEL), IS_HEAD, and the head's fabric
# (FABRIC_IF, IB_HCAS) when the inventory records it.
rack_resolve_site() {
  local head="" v
  rack_resolve_workers
  IS_HEAD=0; rack_is_head && IS_HEAD=1
  if inv_exists; then
    if [ "$IS_HEAD" = 1 ]; then head=$(inv_local_name); else head=$(inv_head_name); fi
  fi
  if [ -n "$head" ]; then
    v=$(inv_get "$head" NODE_FABRIC_IP); [ -n "$v" ] && HEAD_IP=$v
    v=$(inv_get "$head" NODE_FABRIC_IF); [ -n "$v" ] && FABRIC_IF=$v
    v=$(inv_get "$head" NODE_IB_HCAS);   [ -n "$v" ] && IB_HCAS=$v
    [ -n "${HEAD_SSH:-}" ] || HEAD_SSH=$(inv_get "$head" NODE_SSH)
  fi
  if [ -z "${HEAD_LABEL:-}" ]; then
    if [ -n "$head" ]; then HEAD_LABEL=$head
    elif [ "$IS_HEAD" = 1 ] && legacy_rack; then HEAD_LABEL=$LEGACY_HEAD
    elif [ "$IS_HEAD" = 1 ] && ! is_localhost "${HEAD_SSH:-}"; then HEAD_LABEL=$HEAD_SSH
    elif [ "$IS_HEAD" = 1 ]; then HEAD_LABEL=$(local_label)
    else HEAD_LABEL=${HEAD_SSH:-$LEGACY_HEAD}
    fi
  fi
  [ "$IS_HEAD" = 1 ] || HEAD_SSH=${HEAD_SSH:-$LEGACY_HEAD}
  if [ -z "${HEAD_IP:-}" ] && legacy_rack; then HEAD_IP=$LEGACY_HEAD_IP; fi
  return 0
}

# ------------------------------------------------------------------- json --
inv_node_json() {
  local n=$1 k sep="" v
  printf '{'
  for k in $INV_KEYS; do
    v=$(inv_get "$n" "$k")
    printf '%s%s:%s' "$sep" "$(json_str "$(printf '%s' "${k#NODE_}" | tr '[:upper:]' '[:lower:]')")" "$(json_str "$v")"
    sep=,
  done
  printf '}'
}
inv_json() {
  local n sep=""
  printf '{"schema":%s,"nodes":[' "$RACK_JSON_SCHEMA"
  for n in $(inv_names); do printf '%s' "$sep"; inv_node_json "$n"; sep=,; done
  printf ']}\n'
}
