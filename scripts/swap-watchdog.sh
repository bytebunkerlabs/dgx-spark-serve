#!/usr/bin/env bash
# Replace any running memwatch for $1 with an OBSERVE-mode one. Engine untouched.
# Runs from a FILE on purpose: an inline `ssh host 'pgrep -f memwatch...; nohup memwatch.sh ...'`
# has the pattern in its own argv, so pgrep matches the calling shell and `kill`
# ends the ssh session (exit 255) — happened twice on 2026-09-06. A script's
# argv is just its path.
# Usage: swap-watchdog.sh <container> <path/to/memwatch.sh>   (MEMWATCH_KILL=1 to re-arm killing)
C=${1:?container}; MW=${2:?path to memwatch.sh}
old=$(pgrep -f "[m]emwatch.sh $C" || true)
# shellcheck disable=SC2086  # one or more pids
if [ -n "$old" ]; then kill $old; echo "  $(hostname): stopped old watchdog pid(s) $old"; else echo "  $(hostname): no old watchdog"; fi
sleep 1
logs=$HOME/.local/state/dgx-serve/logs; mkdir -p "$logs"
MEMWATCH_KILL=${MEMWATCH_KILL:-0} nohup "$MW" "$C" 3 > "$logs/memwatch-$C-observe-$(date +%Y%m%d-%H%M%S).log" 2>&1 &
sleep 2
# shellcheck disable=SC2012  # our own timestamped names
grep -m1 "memwatch start" "$(ls -t "$logs"/memwatch-"$C"-observe-*.log | head -1)" | sed "s/^/  $(hostname) now: /"
