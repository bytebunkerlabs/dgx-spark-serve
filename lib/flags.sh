# shellcheck shell=bash disable=SC2034  # the parsed flags are for the commands that source this
# lib/flags.sh: the flags the serving commands share.
#
#   --dgx --linux --windows --mac   which platform's variant; default: this machine's
#   --plan                          print what would run, and run nothing
#   --json                          machine-readable output
#   --on <node>                     the same command on an inventory node, over ssh
#                                   (handled by rack before any command runs)
#
# A platform flag this machine cannot honour is refused with the reason and
# the fix, except under --plan: planning works anywhere. Bash 3.2.

platform_title() { # "a DGX Spark": how a platform reads in a sentence
  case "$1" in
    dgx) printf 'a DGX Spark' ;;
    linux) printf 'a Linux machine with an NVIDIA GPU' ;;
    windows) printf 'a Windows PC (WSL2)' ;;
    mac) printf 'a Mac' ;;
    *) printf 'a machine dgx-serve cannot serve on' ;;
  esac
}
platform_the() { platform_title "$1" | sed 's/^an* /the /'; }

# flags_parse "$@": take the shared flags out of the arguments. Sets
# WANT_PLATFORM (empty: this machine's), PLAN, JSON, and REST (the others, in
# order; expand with ${REST[@]+"${REST[@]}"} under set -u on bash 3.2).
flags_parse() {
  WANT_PLATFORM="" PLAN=0 JSON=0 REST=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --dgx|--linux|--windows|--mac)
        [ -z "$WANT_PLATFORM" ] || [ "$WANT_PLATFORM" = "${1#--}" ] \
          || die "one platform at a time: --$WANT_PLATFORM or $1"
        WANT_PLATFORM=${1#--} ;;
      --plan) PLAN=1 ;;
      --json) JSON=1 ;;
      --) shift; while [ $# -gt 0 ]; do REST+=("$1"); shift; done; break ;;
      *) REST+=("$1") ;;
    esac
    shift
  done
  return 0
}

# This machine's platform: what rack init recorded, else detected now.
this_platform() {
  local p=""
  inv_exists && p=$(inv_get "$(inv_local_name)" NODE_PLATFORM)
  if [ -z "$p" ]; then platform_detect; p=$PLATFORM; fi
  printf '%s' "$p"
}

# flags_target <command>: settle the platform a command works for, as
# TARGET_PLATFORM. Refuses a platform flag this machine is not, saying what
# it is, what the flag needs, and the way to get there.
flags_target() {
  local cmd=$1 here
  here=$(this_platform)
  TARGET_PLATFORM=${WANT_PLATFORM:-$here}
  [ "$TARGET_PLATFORM" = "$here" ] && return 0
  [ "$PLAN" = 1 ] && return 0
  [ "$TARGET_PLATFORM" = unsupported ] && die "this is $(platform_title unsupported): ${PLAT_REASON:-rack platform says why}"
  die "$(platform_refusal "$here" "$TARGET_PLATFORM" "$cmd")"
}

# "this is a DGX Spark; --mac needs a Mac: run it there, or add the Mac with
# rack nodes add and use --on" -- naming the node when the inventory has one.
platform_refusal() {
  local here=$1 want=$2 cmd=$3 n node=""
  for n in $(inv_names); do
    [ "$(inv_get "$n" NODE_LOCAL)" = 1 ] && continue
    [ "$(inv_get "$n" NODE_PLATFORM)" = "$want" ] && { node=$n; break; }
  done
  printf 'this is %s; --%s needs %s: run it there' "$(platform_title "$here")" "$want" "$(platform_title "$want")"
  if [ -n "$node" ]; then printf ', or use --on %s (rack %s ... --on %s)' "$node" "$cmd" "$node"
  else printf ', or add %s with rack nodes add and use --on' "$(platform_the "$want")"; fi
  printf '. --plan shows what would run, on any machine.'
}

# run_on_node <node> <rack args...>: the same rack command on that node, over
# ssh, from its own checkout; replaces this process. Returns 1 when the node
# is this machine (run it here).
#
# The node's rack must speak this rack's language first: a rack from before
# 1.0 ignores flags it does not know, so `rack up x --plan` there would
# launch instead of plan. One that cannot answer `rack version --json` with
# this JSON schema is refused before anything runs.
run_on_node() {
  local n=$1 t dir tty=""; shift
  inv_has "$n" || die "no such node: $n  (rack nodes)"
  [ "$(inv_get "$n" NODE_LOCAL)" = 1 ] && return 1
  t=$(node_ssh "$n") dir=$(node_rack_dir "$n")
  [ -t 0 ] && [ -t 1 ] && tty=-t
  exec ssh $tty -o BatchMode=yes -o ConnectTimeout=10 "$t" \
    "cd $(printf %q "$dir") 2>/dev/null || { echo 'rack is not installed at ~/$dir on $n: install dgx-serve there, or rack nodes add $n --rack-dir <path>' >&2; exit 2; }
./rack version --json 2>/dev/null | grep -q '\"json_schema\":${RACK_JSON_SCHEMA}[,}]' || { echo 'the rack on $n is older than this one (no rack version --json): update dgx-serve there. Nothing was run.' >&2; exit 3; }
exec ./rack $(printf '%q ' "$@")"
}
