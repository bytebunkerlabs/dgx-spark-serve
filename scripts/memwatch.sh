#!/usr/bin/env bash
# memwatch.sh <container> [min_avail_gib] [psi_full_pct]
#
# Host-memory watchdog for a GB10 engine container; rack up starts one per
# DGX Spark, next to the engine (logs: ~/.local/state/dgx-serve/logs). On
# unified memory an exhausted pool does NOT raise a clean OOM: the kernel
# enters reclaim stall, userspace (sshd, tailscaled) starves, and the node
# has to be power-cycled —
# this rack lost 10 days to it on 2026-08-26 and wedged twice more on 09-06.
#
# THE METRIC IS PSI, NOT A MemAvailable FLOOR. /proc/pressure/memory "full
# avg10" is the kernel's own measure of the fraction of the last 10 s in which
# ALL tasks were stalled on reclaim — the livelock itself, measured directly.
# MemAvailable cannot tell a healthy KV-cache allocation (drops ~20 GiB in 6 s,
# PSI ~0) from a runaway; four healthy launches were killed on 09-06 by floors
# of 8 and 4 GiB while the Incident-2 wedge sat at 16-18 GiB "available" until
# it died. History, so nobody re-learns it.
#
# MEASURED BASELINE (09-06, an ~80 GB TP=2 load phase, healthy): PSI full
# avg10 = 17-22% with MemAvailable 22-28 GiB and some == full — ONE task (the
# shard loader) stalling on its own file pages under the cgroup cap. That is
# the normal cost of reading ~80 GB through a pinned page cache, not danger.
# A standalone 20% PSI trigger killed that launch (the 5th false positive).
#
# CALIBRATION (09-06 evening, first full HEALTHY launch observed end to end,
# a ~100 GB NVFP4 MoE at GMU 0.80, 142 ticks):
#   head   min MemAvailable 7,337 MiB   max PSI full avg10 36.1%   (LiteLLM +
#          monitoring + API server live here; steady state serving = ~7 GiB)
#   worker min MemAvailable 10,882 MiB  max PSI full avg10 12.6%
# The earlier conjunction (PSI>20 AND avail<12 GiB) WOULD have fired on this
# successful run at 13:12:10 — observe mode is the only reason it served.
# Thresholds below sit clear of the healthy envelope: hard 60% (healthy peak
# 36), stall 45% AND < 5 GiB (healthy floor 7.3), backstop 3 GiB.
#
# Triggers (any):
#   PSI full avg10 > psi_full_pct (default 45) AND MemAvailable < 5 GiB   (stall + memory gone)
#   PSI full avg10 > 60%                                                  (deep livelock, any avail)
#   MemAvailable < min_avail_gib (default 3)                              (backstop)
# MEMWATCH_KILL=0 (the launcher default until calibrated on a full successful
# run) makes it log-only. Timeline every 5 s — THIS is the data the thresholds
# should have come from. The cgroup cap (MEM_CAP_GB) is the hard bound.
CONTAINER="${1:?container}"; MIN_AVAIL_GIB="${2:-3}"; PSI_PCT="${3:-45}"
KILL="${MEMWATCH_KILL:-0}"
STALL_AVAIL_KB=$((5*1048576)); PSI_HARD=60
MIN_AVAIL_KB=$(awk -v g="$MIN_AVAIL_GIB" 'BEGIN{printf "%d", g*1048576}')
PSI=/proc/pressure/memory
[ -r "$PSI" ] || echo "$(date '+%F %T') WARNING: $PSI not readable — PSI trigger disabled, MemAvailable backstop only"
echo "$(date '+%F %T') memwatch start: container=$CONTAINER psi_full_avg10>${PSI_PCT}% avail<${MIN_AVAIL_GIB}GiB kill=$KILL"
for _ in $(seq 1 120); do docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}\$" && break; sleep 1; done
tick=0
while docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}\$"; do
  avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  free=$(awk '/MemFree/{print $2}' /proc/meminfo)
  psi_full=$( [ -r "$PSI" ] && awk '/^full/{for(i=1;i<=NF;i++) if($i ~ /^avg10=/){sub("avg10=","",$i); print $i}}' "$PSI" || echo 0 )
  psi_some=$( [ -r "$PSI" ] && awk '/^some/{for(i=1;i<=NF;i++) if($i ~ /^avg10=/){sub("avg10=","",$i); print $i}}' "$PSI" || echo 0 )
  reason=""
  if awk -v p="$psi_full" -v t="$PSI_HARD" 'BEGIN{exit !(p>t)}'; then reason="PSI full avg10=${psi_full}% > ${PSI_HARD}% (deep livelock)"
  elif awk -v p="$psi_full" -v t="$PSI_PCT" 'BEGIN{exit !(p>t)}' && (( avail < STALL_AVAIL_KB )); then reason="PSI full avg10=${psi_full}% > ${PSI_PCT}% AND MemAvailable $((avail/1024))MiB < 5GiB (stall with memory gone)"
  elif (( avail < MIN_AVAIL_KB )); then reason="MemAvailable $((avail/1024))MiB < ${MIN_AVAIL_GIB}GiB (backstop)"; fi
  if [ -n "$reason" ]; then
    echo "$(date '+%F %T') TRIGGER: $reason  [avail=$((avail/1024))MiB free=$((free/1024))MiB psi some/full=${psi_some}/${psi_full}%]"
    if [ "$KILL" = 1 ]; then
      docker kill "$CONTAINER" >/dev/null 2>&1
      echo "$(date '+%F %T') killed $CONTAINER — launch aborted to keep this node reachable"; exit 2
    else
      echo "$(date '+%F %T') OBSERVE MODE (MEMWATCH_KILL=0): not killing"
    fi
  fi
  if (( tick % 5 == 0 )); then echo "$(date '+%T') avail=$((avail/1024))MiB free=$((free/1024))MiB psi some/full=${psi_some}/${psi_full}%"; fi
  tick=$((tick+1)); sleep 1
done
echo "$(date '+%F %T') container gone; memwatch exit"
