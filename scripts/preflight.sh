#!/usr/bin/env bash
# Read-only sanity check of one Spark. Run on BOTH nodes before any launch.
# Everything checked here is something a launch dies without — usually with an
# error that looks like a model problem instead of what it really is.
set -u
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && . ./.env
HEAD_IP=${HEAD_IP:-192.168.100.1}
WORKER_IP=${WORKER_IP:-192.168.100.2}
FABRIC_IF=${FABRIC_IF:-enp1s0f0np0}
FABRIC_IF2=${FABRIC_IF2:-enP2p1s0f0np0}
RDMA_DEV=${RDMA_DEV:-rocep1s0f0}
HF_CACHE=${HF_CACHE:-$HOME/dgx/hf}

P=0; F=0; W=0
ok() { printf 'PASS  %s\n' "$1"; P=$((P+1)); }
no() { printf 'FAIL  %s\n' "$1"; F=$((F+1)); }
wr() { printf 'warn  %s\n' "$1"; W=$((W+1)); }

# --- the box -----------------------------------------------------------------
[ "$(uname -m)" = aarch64 ] && ok "aarch64" || no "not aarch64 — is this a Spark?"
command -v nvidia-smi >/dev/null 2>&1 && ok "nvidia-smi present" || no "nvidia-smi missing"

if docker info >/dev/null 2>&1; then
  if docker info 2>/dev/null | grep -qi nvidia; then
    ok "docker up, nvidia runtime registered"
  else
    no "docker up but no nvidia runtime — GPU containers will not start"
  fi
else
  no "docker daemon unreachable (group membership? service down?)"
fi

# --- memory ------------------------------------------------------------------
# Unified pool: free(1) is the truth. nvidia-smi reports "Not Supported" here.
avail=$(free -g | awk '/^Mem:/{print $7}')
if [ "${avail:-0}" -ge 100 ]; then
  ok "memory: ${avail} GiB available"
elif [ "${avail:-0}" -ge 20 ]; then
  wr "memory: ${avail} GiB available — stop the current stack and drop caches before a big load"
else
  wr "memory: ${avail} GiB available — something large is resident (serving stack? page cache?)"
fi

if systemctl is-active --quiet earlyoom 2>/dev/null; then
  wr "earlyoom active — it kills workers under transient load pressure; disable for cluster runs"
else
  ok "earlyoom not active"
fi

# --- fabric: two rails, one port ---------------------------------------------
for i in "$FABRIC_IF" "$FABRIC_IF2"; do
  if [ -e "/sys/class/net/$i" ]; then
    c=$(cat "/sys/class/net/$i/carrier" 2>/dev/null)
    m=$(cat "/sys/class/net/$i/mtu" 2>/dev/null)
    a=$(ip -4 -br addr show "$i" | awk '{print $3}')
    if [ "$c" != "1" ]; then
      no "$i: no carrier (cable? far end down?)"
      continue
    fi
    [ "$m" = "9000" ] || wr "$i: mtu $m, expected 9000"
    if [ -n "$a" ]; then
      ok "$i: up, mtu $m, $a"
    else
      no "$i: carrier but no IPv4 — this rail is idle, you have half the fabric"
    fi
  else
    no "$i: interface not found"
  fi
done

s1=$(cat "/sys/class/net/$FABRIC_IF/phys_switch_id" 2>/dev/null)
s2=$(cat "/sys/class/net/$FABRIC_IF2/phys_switch_id" 2>/dev/null)
if [ -n "$s1" ] && [ "$s1" = "$s2" ]; then
  ok "rails share phys_switch_id — one port, two PCIe paths, as designed"
else
  wr "rails do not share phys_switch_id — check the interface names in .env"
fi

# --- RDMA + GID --------------------------------------------------------------
[ -d "/sys/class/infiniband/$RDMA_DEV" ] && ok "RDMA device $RDMA_DEV present" \
  || no "RDMA device $RDMA_DEV missing"

g=$("$(dirname "$0")/gid-index.sh" "$RDMA_DEV" 2>/dev/null)
if [ -n "${g:-}" ]; then
  ok "RoCE v2 GID index resolves to $g (resolved fresh — never hardcoded)"
else
  no "no RoCE v2 GID matches this interface's IP — NCCL will fail with ENODATA"
fi

# --- firewall ----------------------------------------------------------------
# The classic silent killer: ICMP passes, RDMA hangs forever.
if command -v ufw >/dev/null 2>&1 && sudo -n ufw status >/dev/null 2>&1; then
  for i in "$FABRIC_IF" "$FABRIC_IF2"; do
    if sudo -n ufw status | grep -q "Anywhere on $i"; then
      ok "ufw allows in on $i"
    else
      no "ufw has no allow rule for $i — ping will work, RDMA will hang"
    fi
  done
else
  wr "ufw rules not checked (needs passwordless sudo) — verify 'allow in on' both fabric interfaces"
fi

# --- the peer ----------------------------------------------------------------
me=$(ip -4 -br addr show "$FABRIC_IF" 2>/dev/null | awk '{print $3}' | cut -d/ -f1)
peer=$WORKER_IP; [ "$me" = "$WORKER_IP" ] && peer=$HEAD_IP
if ping -c1 -W2 "$peer" >/dev/null 2>&1; then
  ok "peer $peer reachable over the fabric"
else
  no "peer $peer unreachable over the fabric"
fi
if ssh -o BatchMode=yes -o ConnectTimeout=4 "$peer" true 2>/dev/null; then
  ok "passwordless ssh to $peer"
else
  wr "no passwordless ssh to $peer (required on the head; harmless on the worker)"
fi

# --- disk --------------------------------------------------------------------
da=$(df -BG "$HF_CACHE" 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "${da:-0}" -ge 200 ]; then
  ok "disk: ${da} GB free at $HF_CACHE"
else
  wr "disk: ${da:-?} GB free at $HF_CACHE — Inkling alone is 171 GB"
fi

printf '\n%d pass, %d warn, %d fail\n' "$P" "$W" "$F"
[ "$F" -eq 0 ]
