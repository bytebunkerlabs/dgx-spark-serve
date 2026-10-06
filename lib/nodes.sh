# shellcheck shell=bash
# lib/nodes.sh: the rack init and rack nodes commands. Sourced by rack after
# lib/inventory.sh; uses rack's resolved site (IS_HEAD, HEAD_LABEL,
# WORKER_NAMES...) and ROOT. Bash 3.2.

# A node's `rack platform --json`, asked over ssh. rack need not be installed
# there: the probe ships lib/platform.sh on stdin. Extra lines after the JSON
# answer the extra checks (nodes test). Prints nothing when ssh fails.
remote_probe() { # remote_probe <ssh-target> [extra bash...]
  local target=$1; shift
  { cat "${RACK_ROOT:-.}/lib/common.sh" "${RACK_ROOT:-.}/lib/platform.sh"
    printf '\nplatform_detect\nplatform_json\n'
    [ $# -gt 0 ] && printf '%s\n' "$@"
  } | ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" 'bash -s' 2>/dev/null || true
}

valid_ipv4() { printf '%s' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }

need_value() { [ -n "${2:-}" ] && [ "${2#-}" = "$2" ] || die "$1 needs a value"; }

cmd_nodes() {
  local sub=${1:-ls}
  [ $# -gt 0 ] && shift
  case "$sub" in
    ls|list) nodes_ls "$@" ;;
    --json)  nodes_ls --json ;;
    add)     nodes_add "$@" ;;
    rm|remove) nodes_rm "$@" ;;
    test)    nodes_test "$@" ;;
    *) die "usage: rack nodes [ls|add|rm|test]  (rack --help)" ;;
  esac
}

nodes_ls() {
  case "${1:-}" in --json) inv_json; return ;; '') ;; *) die "usage: rack nodes ls [--json]" ;; esac
  inv_exists || { dim "no inventory yet: run rack init on this machine, then rack nodes add <name>"; return 0; }
  local n reach fab
  printf '  %-16s %-7s %-9s %-26s %s\n' NODE ROLE PLATFORM REACHED FABRIC
  for n in $(inv_names); do
    if [ "$(inv_get "$n" NODE_LOCAL)" = 1 ]; then reach="this machine"; else reach="ssh $(node_ssh "$n")"; fi
    fab=$(inv_get "$n" NODE_FABRIC_IP)
    [ -n "$fab" ] && [ -n "$(inv_get "$n" NODE_FABRIC_IF)" ] && fab="$fab on $(inv_get "$n" NODE_FABRIC_IF)"
    printf '  %-16s %-7s %-9s %-26s %s\n' "$n" "$(inv_get "$n" NODE_ROLE)" "$(inv_get "$n" NODE_PLATFORM)" "$reach" "${fab:--}"
  done
}

nodes_add() {
  local name="" target="" fabric="" fif="" hcas="" role="" plat="" dir="" cache="" gpus="" probe=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --ssh)       need_value "$1" "${2:-}"; target=$2; shift 2 ;;
      --fabric)    need_value "$1" "${2:-}"; fabric=$2; shift 2 ;;
      --fabric-if) need_value "$1" "${2:-}"; fif=$2; shift 2 ;;
      --hcas)      need_value "$1" "${2:-}"; hcas=$2; shift 2 ;;
      --role)      need_value "$1" "${2:-}"; role=$2; shift 2 ;;
      --platform)  need_value "$1" "${2:-}"; plat=$2; shift 2 ;;
      --dgx|--linux|--windows|--mac) plat=${1#--}; shift ;;
      --rack-dir)  need_value "$1" "${2:-}"; dir=${2#\~/}; shift 2 ;;
      --hf-cache)  need_value "$1" "${2:-}"; cache=$2; shift 2 ;;
      --gpus)      need_value "$1" "${2:-}"; gpus=$2; shift 2 ;;
      --no-probe)  probe=0; shift ;;
      -*) die "rack nodes add: unknown option $1  (rack --help)" ;;
      *) [ -z "$name" ] || die "rack nodes add: one node at a time"; name=$1; shift ;;
    esac
  done
  [ -n "$name" ] || die "usage: rack nodes add <name> [--ssh user@host] [--fabric <ip>] [--role worker|node] [--dgx|--linux|--windows|--mac]"
  inv_valid_name "$name" || die "node names are letters, digits, dot, dash and underscore: '$name'"
  [ "$(inv_get "$name" NODE_LOCAL)" = 1 ] && die "$name is this machine: rack init keeps its entry"
  case "$plat" in ''|dgx|linux|windows|mac) ;; *) die "--platform is dgx, linux, windows or mac (got: $plat)" ;; esac
  case "$role" in ''|head|worker|node) ;; *) die "--role is worker, node or head (got: $role)" ;; esac
  [ -z "$fabric" ] || valid_ipv4 "$fabric" || die "--fabric takes the node's IPv4 address on the fabric (got: $fabric)"
  case "$gpus" in ''|*[!0-9]*) [ -z "$gpus" ] || die "--gpus takes a number (got: $gpus)" ;; esac
  # A node that exists is updated: only what was given changes.
  local existing=0; inv_has "$name" && existing=1
  [ -n "$target" ] || target=$(inv_get "$name" NODE_SSH)
  [ -n "$target" ] || target=$name
  if [ -z "$role" ]; then
    role=$(inv_get "$name" NODE_ROLE)
    # On the fabric it is a head's tensor-parallel peer; otherwise it serves on its own.
    [ -n "$role" ] || { [ -n "$fabric" ] && role=worker || role=node; }
  fi
  local head; head=$(inv_head_name)
  [ "$role" = head ] && [ -n "$head" ] && [ "$head" != "$name" ] \
    && die "$head is already the head: a rack has one (rack nodes rm $head, or --role worker)"

  local seen="" gpus_seen=""
  if [ "$probe" = 1 ]; then
    local j; j=$(remote_probe "$target" | grep '^{"schema"' | tail -1 || true)
    if [ -n "$j" ]; then
      seen=$(printf '%s' "$j" | json_get platform)
      gpus_seen=$(printf '%s' "$j" | json_get gpus)
      [ "$seen" = unsupported ] && die "$name cannot serve: $(printf '%s' "$j" | json_get reason)"
      [ -z "$plat" ] || [ "$plat" = "$seen" ] || die "$name says it is $seen, not $plat (rack platform, run there)"
      plat=$seen
    elif [ -z "$plat" ] && [ "$existing" = 0 ]; then
      die "cannot reach $target over ssh without a password.
  Set up key-based ssh first (ssh-copy-id $target), or record it anyway:
  rack nodes add $name --ssh $target --<platform> --no-probe"
    else
      warn "cannot reach $target over ssh; recorded without checking (rack nodes test $name)"
    fi
  fi
  [ -n "$plat" ] || plat=$(inv_get "$name" NODE_PLATFORM)
  [ -n "$plat" ] || die "rack nodes add: say which platform $name is (--dgx, --linux, --windows or --mac)"
  if [ "$role" = worker ] && [ -n "$head" ] && [ "$head" != "$name" ]; then
    local hp; hp=$(inv_get "$head" NODE_PLATFORM)
    [ -z "$hp" ] || [ "$hp" = "$plat" ] || die "$name is $plat but the head, $head, is $hp: one model across machines needs the same platform on each (--role node serves on its own)"
    case "$plat" in mac|windows) die "one model across machines is a DGX and Linux feature: add $name with --role node and serve on it with --on $name" ;; esac
  fi

  local kv="NODE_SSH=$target NODE_PLATFORM=$plat NODE_ROLE=$role NODE_LOCAL=0"
  [ -n "$fabric" ] && kv="$kv NODE_FABRIC_IP=$fabric"
  [ -n "$fif" ] && kv="$kv NODE_FABRIC_IF=$fif"
  [ -n "$hcas" ] && kv="$kv NODE_IB_HCAS=$hcas"
  [ -n "$dir" ] && kv="$kv NODE_RACK_DIR=$dir"
  [ -n "$cache" ] && kv="$kv NODE_HF_CACHE=$cache"
  gpus=${gpus:-$gpus_seen}
  [ -n "$gpus" ] && kv="$kv NODE_GPUS=$gpus"
  # The head's own fabric address is the one it reaches the worker from.
  if [ "$role" = worker ] && [ -n "$fabric" ] && [ -n "$head" ] && [ "$(inv_get "$head" NODE_LOCAL)" = 1 ] \
     && [ -z "$(inv_get "$head" NODE_FABRIC_IP)" ]; then
    local src dev
    src=$(ip -o route get "$fabric" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1 || true)
    dev=$(ip -o route get "$fabric" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1 || true)
    if [ -n "$src" ]; then
      inv_set "$head" NODE_FABRIC_IP="$src" ${dev:+NODE_FABRIC_IF=$dev}
      dim "  $head reaches $fabric from $src${dev:+ on $dev}: recorded as its fabric address"
    else
      warn "  no route from $head to $fabric yet: rack init --fabric <this machine's fabric ip>"
    fi
  fi
  # A worker inherits the head's fabric interface and HCA names unless told:
  # identical machines name them identically (the Sparks do).
  if [ "$role" = worker ] && [ -n "$head" ]; then
    [ -n "$fif$(inv_get "$name" NODE_FABRIC_IF)" ] || [ -z "$(inv_get "$head" NODE_FABRIC_IF)" ] || kv="$kv NODE_FABRIC_IF=$(inv_get "$head" NODE_FABRIC_IF)"
    [ -n "$hcas$(inv_get "$name" NODE_IB_HCAS)" ] || [ -z "$(inv_get "$head" NODE_IB_HCAS)" ] || kv="$kv NODE_IB_HCAS=$(inv_get "$head" NODE_IB_HCAS)"
  fi
  # shellcheck disable=SC2086  # kv holds KEY=VALUE words, checked by inv_set
  inv_set "$name" $kv
  if [ "$existing" = 1 ]; then bold "updated $name"; else bold "added $name"; fi
  [ -n "$head" ] || [ "$role" = head ] || dim "  no head in the inventory yet: rack init on the machine that fronts this rack"
  [ "$role" != worker ] || [ -n "$(inv_get "$name" NODE_FABRIC_IP)" ] \
    || warn "  $name is a worker with no fabric address: rack nodes add $name --fabric <ip>"
  nodes_ls
}

nodes_rm() {
  [ $# -eq 1 ] || die "usage: rack nodes rm <name>"
  inv_has "$1" || die "no such node: $1  (rack nodes)"
  [ "$(inv_get "$1" NODE_LOCAL)" = 1 ] && warn "$1 is this machine: rack init adds it back"
  inv_rm "$1"
  bold "removed $1 from the inventory (nothing on $1 itself was touched)"
}

# Can rack drive each node? ssh without a password, the platform it was
# recorded as, rack installed where the inventory says, and the fabric up.
nodes_test() {
  local json=0 names="" n a bad=0 sep="" out=""
  for a in "$@"; do
    case "$a" in --json) json=1 ;; -*) die "usage: rack nodes test [<name>...] [--json]" ;; *) names="$names $a" ;; esac
  done
  inv_exists || die "no inventory yet: rack init"
  [ -n "$names" ] || names=$(inv_names)
  for n in $names; do inv_has "$n" || die "no such node: $n  (rack nodes)"; done
  local mine; mine=$(inv_get "$(inv_local_name)" NODE_FABRIC_IP)
  for n in $names; do
    local want plat="" s_ssh s_plat s_rack s_fab fip j dir note=""
    want=$(inv_get "$n" NODE_PLATFORM) fip=$(inv_get "$n" NODE_FABRIC_IP)
    s_rack=ok s_fab=none
    if [ "$(inv_get "$n" NODE_LOCAL)" = 1 ]; then
      s_ssh=local
      platform_detect; plat=$PLATFORM
      [ -z "$fip" ] || { has_local_ip "$fip" && s_fab=ok || s_fab=missing; }
    else
      dir=$(node_rack_dir "$n")
      j=$(remote_probe "$(node_ssh "$n")" \
            "[ -x $(printf %q "$dir")/rack ] && echo RACK=ok || echo RACK=missing" \
            "$([ -n "$fip" ] && printf 'has_local_ip %q && echo FABRIC=ok || echo FABRIC=missing' "$fip")")
      if printf '%s\n' "$j" | grep -q '^{"schema"'; then
        s_ssh=ok
        plat=$(printf '%s\n' "$j" | grep '^{"schema"' | tail -1 | json_get platform)
        s_rack=$(printf '%s\n' "$j" | sed -n 's/^RACK=//p' | tail -1)
        [ -n "$s_rack" ] || s_rack=missing
        if [ -n "$fip" ]; then
          s_fab=$(printf '%s\n' "$j" | sed -n 's/^FABRIC=//p' | tail -1)
          [ -n "$s_fab" ] || s_fab=missing
          # Owning the address is half of it; this machine must reach it too.
          [ "$s_fab" = ok ] && [ -n "$mine" ] && ! ping_once "$fip" && s_fab=unreachable
        fi
      else
        s_ssh=fail s_rack=unknown
        [ -n "$fip" ] && s_fab=unknown
        note="no passwordless ssh to $(node_ssh "$n"): ssh-copy-id $(node_ssh "$n")"
      fi
    fi
    if [ -z "$plat" ]; then s_plat=unknown
    elif [ "$plat" = "$want" ]; then s_plat=ok
    else s_plat=mismatch; note="${note:+$note; }recorded as $want, but it is $plat: rack nodes add $n --$plat"; fi
    [ "$s_rack" = missing ] && note="${note:+$note; }rack is not installed at ~/$(node_rack_dir "$n") there (rack nodes add $n --rack-dir <path>)"
    case "$s_fab" in missing) note="${note:+$note; }$fip is not on any of its interfaces" ;;
      unreachable) note="${note:+$note; }this machine cannot reach $fip: rack preflight" ;; esac
    local good=1
    case "$s_ssh$s_plat$s_rack" in *fail*|*mismatch*|*missing*|*unknown*) good=0 ;; esac
    case "$s_fab" in ok|none) ;; *) good=0 ;; esac
    [ $good = 1 ] || bad=1
    if [ $json = 1 ]; then
      out="$out$sep{\"name\":$(json_str "$n"),\"ok\":$(json_bool $good),\"ssh\":$(json_str "$s_ssh"),\"platform\":$(json_str "$plat"),\"platform_recorded\":$(json_str "$want"),\"platform_check\":$(json_str "$s_plat"),\"rack\":$(json_str "$s_rack"),\"fabric\":$(json_str "$s_fab"),\"note\":$(json_str "$note")}"
      sep=,
    else
      printf '  %-16s %-5s ssh %-6s platform %-8s rack %-8s fabric %s\n' "$n" "$([ $good = 1 ] && echo ok || echo FAIL)" \
        "$s_ssh" "${plat:-?}" "$s_rack" "$s_fab"
      [ -z "$note" ] || dim "      $note"
    fi
  done
  if [ $json = 1 ]; then printf '{"schema":%s,"ok":%s,"nodes":[%s]}\n' "$RACK_JSON_SCHEMA" "$(json_bool $((1 - bad)))" "$out"; fi
  [ $bad = 0 ]
}

# --------------------------------------------------------------- init ------
# rack init: make this machine part of a rack. Detects the platform (and
# refuses one dgx-serve cannot serve on), writes this machine's inventory
# entry, imports the head/worker pair from .env the first time, creates the
# engine key, and checks what serving here needs, each with its fix.
INIT_CHECKS=""
init_check() { INIT_CHECKS="$INIT_CHECKS$1	$2	$3
"; }   # init_check ok|warn|fail <what> <detail or fix>

cmd_init() {
  local name="" force=0 json=0 set_fip="" set_fif="" set_hcas=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name) need_value "$1" "${2:-}"; name=$2; shift 2 ;;
      --fabric) need_value "$1" "${2:-}"; set_fip=$2; shift 2 ;;
      --fabric-if) need_value "$1" "${2:-}"; set_fif=$2; shift 2 ;;
      --hcas) need_value "$1" "${2:-}"; set_hcas=$2; shift 2 ;;
      --force) force=1; shift ;;
      --json) json=1; shift ;;
      *) die "usage: rack init [--name <name>] [--fabric <ip> [--fabric-if <if>] [--hcas <a,b>]] [--force] [--json]" ;;
    esac
  done
  if [ -n "$set_fip" ]; then
    valid_ipv4 "$set_fip" || die "--fabric takes this machine's IPv4 address on the fabric (got: $set_fip)"
    has_local_ip "$set_fip" || die "$set_fip is not an address of this machine"
  fi
  platform_detect
  platform_support_check
  [ "$PLATFORM" = unsupported ] && die "rack init: dgx-serve cannot serve on this machine: $PLAT_REASON"
  if [ "$PLAT_SUPPORTED" != 1 ]; then
    [ $force = 1 ] || die "rack init: $PLAT_SUPPORT_NOTE.
  dgx-serve 1.0 supports DGX OS; Ubuntu 22.04 or 24.04 with an NVIDIA GPU; Ubuntu 24.04
  in WSL2 on Windows 11; and macOS 14 or later on Apple Silicon.
  rack init --force goes ahead anyway, unsupported."
    warn "going ahead on an unsupported system: $PLAT_SUPPORT_NOTE"
  fi
  if ! inv_exists && [ "$IS_HEAD" != 1 ]; then
    if [ -n "${WORKER_IP:-}" ] && has_local_ip "$WORKER_IP" || { legacy_rack && ! has_local_ip "$LEGACY_HEAD_IP"; }; then
      die "rack init: this machine is a worker of $HEAD_LABEL. Run rack init on $HEAD_LABEL; it adds this machine."
    fi
    die "rack init: this machine drives $HEAD_SSH (HEAD_SSH in .env). Run rack init on $HEAD_SSH,
  or remove HEAD_SSH from .env to serve on this machine."
  fi

  local fresh=0 me created=""
  inv_exists || fresh=1
  me=$(inv_local_name)
  [ -z "$me" ] || [ -z "$name" ] || [ "$name" = "$me" ] \
    || die "this machine is already $me in the inventory (rack nodes rm $me, then rack init --name $name)"
  if [ -z "$me" ]; then
    # named as rack has always called it (spark-1 on the author's rack, HEAD_SSH),
    # else by its hostname
    if [ -n "$name" ]; then me=$name
    elif inv_exists; then me=$(sanitize_name "$(local_label)")
    else me=$(sanitize_name "$HEAD_LABEL"); fi
    inv_valid_name "$me" || die "node names are letters, digits, dot, dash and underscore: '$me' (rack init --name <name>)"
    inv_has "$me" && die "the inventory already has another machine called $me: rack init --name <name>"
    local role=head
    [ -n "$(inv_head_name)" ] && role=node    # a rack elsewhere already has its head
    inv_set "$me" NODE_LOCAL=1 NODE_ROLE=$role NODE_SSH=
    created="$me"
  fi
  inv_set "$me" NODE_PLATFORM="$PLATFORM" NODE_GPUS="$PLAT_GPU_COUNT"

  # The fabric, first time only: the .env (or the author's rack's defaults).
  local fip="" fif="" hcas=""
  if [ "$fresh" = 1 ] && [ -n "${HEAD_IP:-}" ] && has_local_ip "$HEAD_IP"; then
    fip=$HEAD_IP
    fif=${FABRIC_IF:-$(iface_of_ip "$fip")}
    if [ -n "${IB_HCAS:-}" ]; then hcas=$IB_HCAS
    elif legacy_rack; then hcas=rocep1s0f0,roceP2p1s0f0     # both RoCE twins of the cabled port
    fi
    inv_set "$me" NODE_FABRIC_IP="$fip" ${fif:+NODE_FABRIC_IF=$fif} ${hcas:+NODE_IB_HCAS=$hcas}
  fi
  if [ -n "$set_fip" ]; then
    fip=$set_fip fif=${set_fif:-$(iface_of_ip "$set_fip")} hcas=${set_hcas:-$(inv_get "$me" NODE_IB_HCAS)}
    inv_set "$me" NODE_FABRIC_IP="$fip" ${fif:+NODE_FABRIC_IF=$fif} ${hcas:+NODE_IB_HCAS=$hcas}
  fi

  # Workers, first time only: WORKER_SSH from .env, or spark-2 on the author's rack.
  local w wname wip j wplat wgpus
  if [ "$fresh" = 1 ]; then
    for w in $WORKER_NAMES; do
      wname=$(sanitize_name "$w")
      inv_valid_name "$wname" || { warn "skipping worker '$w': not a usable name (rack nodes add <name> --ssh $w)"; continue; }
      wip=""
      [ "$w" = "${WORKER_NAMES%% *}" ] && wip=${WORKER_IP:-}
      [ -z "$wip" ] && legacy_rack && [ "$w" = "$LEGACY_WORKER" ] && wip=$LEGACY_WORKER_IP
      j=$(remote_probe "$w" | grep '^{"schema"' | tail -1 || true)
      wgpus=""
      if [ -n "$j" ]; then wplat=$(printf '%s' "$j" | json_get platform); wgpus=$(printf '%s' "$j" | json_get gpus)
      else wplat=$PLATFORM; warn "could not reach worker $w over ssh: recorded as $PLATFORM, unchecked (rack nodes test)"; fi
      inv_set "$wname" NODE_SSH="$w" NODE_ROLE=worker NODE_LOCAL=0 NODE_PLATFORM="$wplat" ${wgpus:+NODE_GPUS=$wgpus} \
        ${wip:+NODE_FABRIC_IP=$wip} ${fif:+NODE_FABRIC_IF=$fif} ${hcas:+NODE_IB_HCAS=$hcas}
      created="${created:+$created }$wname"
    done
  fi

  # The engine's API key: created once, kept across re-runs, never printed.
  local key_note="kept"
  if [ ! -s "$ENGINE_KEY_FILE" ]; then
    write_secret "$ENGINE_KEY_FILE" "$(new_secret)"
    key_note="created"
  fi
  if [ "$key_note" = created ]; then engine_key_forms new; else engine_key_forms; fi

  init_checks
  local fails all_ok=0
  fails=$(printf '%s' "$INIT_CHECKS" | grep -c '^fail' || true)
  [ "$fails" = 0 ] && all_ok=1
  if [ $json = 1 ]; then
    printf '{"schema":%s,"ok":%s,"node":%s,"created":[' "$RACK_JSON_SCHEMA" "$(json_bool "$all_ok")" "$(json_str "$me")"
    local sep="" c
    for c in $created; do printf '%s%s' "$sep" "$(json_str "$c")"; sep=,; done
    printf '],"engine_key":%s,"engine_key_file":%s,"platform":' "$(json_str "$key_note")" "$(json_str "$ENGINE_KEY_FILE")"
    platform_json | tr -d '\n'
    printf ',"inventory":'
    inv_json | tr -d '\n'
    printf ',"checks":['
    sep=""
    local st what detail
    while IFS='	' read -r st what detail; do
      [ -n "$st" ] || continue
      printf '%s{"status":%s,"check":%s,"detail":%s}' "$sep" "$(json_str "$st")" "$(json_str "$what")" "$(json_str "$detail")"
      sep=,
    done <<EOT
$INIT_CHECKS
EOT
    printf ']}\n'
  else
    bold "$(platform_describe)"
    [ -z "$created" ] || dim "  added to the inventory: $created"
    nodes_ls
    echo
    printf '  engine key  %s (%s; rack up hands it to the engine)\n' "${ENGINE_KEY_FILE/#$HOME/~}" "$key_note"
    echo
    local st what detail
    while IFS='	' read -r st what detail; do
      [ -n "$st" ] || continue
      case "$st" in
        ok)   printf '  ok    %-22s %s\n' "$what" "$detail" ;;
        warn) printf '\033[33m  warn  %-22s %s\033[0m\n' "$what" "$detail" ;;
        fail) printf '\033[31m  FAIL  %-22s %s\033[0m\n' "$what" "$detail" ;;
      esac
    done <<EOT
$INIT_CHECKS
EOT
    echo
    if [ "$fails" = 0 ]; then dim "next: rack recipes, then rack up <recipe>"
    else dim "fix the FAIL lines, then run rack init again (it keeps what it wrote)"; fi
  fi
  [ "$fails" = 0 ]
}

# What serving on this platform needs, each with its fix. Read-only.
init_checks() {
  local v free
  v=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
  if python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then init_check ok python3 "$v"
  else init_check fail python3 "rack needs Python 3.9 or later (found: ${v:-none})"; fi

  case "$PLATFORM" in
    dgx|linux)
      if [ "$PLAT_DOCKER" = 1 ]; then init_check ok docker "running, and $(id -un) can use it"
      else init_check fail docker "not running, or $(id -un) cannot use it: install Docker Engine, then sudo usermod -aG docker $(id -un) and log in again (docs/12-platforms.md)"; fi
      if [ "$PLAT_NVIDIA_RUNTIME" = 1 ]; then init_check ok "NVIDIA container toolkit" "installed"
      else init_check fail "NVIDIA container toolkit" "missing: containers cannot reach the GPU (docs/12-platforms.md, Linux)"; fi ;;
    windows)
      if [ "$PLAT_DOCKER" = 1 ] && [ "$PLAT_NVIDIA_RUNTIME" = 1 ]; then init_check ok runtime "Docker with the GPU, or native"
      else init_check ok runtime "native (a Python venv or llama.cpp; no Docker needed)"; fi ;;
    mac)
      init_check ok runtime "native: llama.cpp with Metal, kept alive by launchd"
      have curl && have tar && init_check ok downloads "curl and tar present" \
        || init_check fail downloads "curl and tar are needed to fetch the llama.cpp build" ;;
  esac

  case "$PLAT_INIT" in
    systemd|launchd) init_check ok "service manager" "$PLAT_INIT" ;;
    *) if [ "$PLATFORM" = windows ]; then
         init_check warn "service manager" "systemd is off in WSL2: add [boot] systemd=true to /etc/wsl.conf, then run wsl --shutdown in Windows"
       else
         init_check warn "service manager" "no systemd: engines restart through Docker, but the cluster boot unit and the bare monitor need it"
       fi ;;
  esac
  if [ "$PLAT_LINGER" = 1 ]; then
    init_check ok linger "user services keep running after logout"
  elif [ "$PLAT_LINGER" = 0 ]; then
    init_check warn linger "user services run only while you are logged in, and a cluster is not re-formed after a reboot: sudo loginctl enable-linger $(id -un)"
  fi

  free=$(disk_free_gb "$HF_CACHE")
  if [ -z "$free" ]; then init_check warn disk "could not read free space at $HF_CACHE"
  elif [ "$free" -ge 100 ]; then init_check ok disk "$free GB free at $HF_CACHE"
  else init_check warn disk "$free GB free at $HF_CACHE: large models need 100 GB or more"; fi

  if [ -s "$DGX_SERVE_CONFIG/hf-token" ] || [ -s "$HOME/.cache/huggingface/token" ] || [ -n "${HF_TOKEN:-}" ]; then
    init_check ok "Hugging Face token" "found (gated models can download)"
  else
    init_check ok "Hugging Face token" "none; only gated models need one ($DGX_SERVE_CONFIG/hf-token)"
  fi

  local n
  for n in $(inv_workers); do
    if node_up "$n"; then init_check ok "ssh $n" "passwordless, as $(node_ssh "$n")"
    else init_check fail "ssh $n" "no passwordless ssh to $(node_ssh "$n"): ssh-copy-id $(node_ssh "$n")"; fi
  done
}
