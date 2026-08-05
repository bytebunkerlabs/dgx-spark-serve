#!/usr/bin/env bash
# Does the serving container talk to the internet? Verdict for "truly local".
#
# Runs INSIDE the container (docker exec) on purpose: the engine runs as
# root, so host-side `ss -p` silently fails to attribute its sockets without
# sudo and reports a false "no connections". In-container we are root, and
# the container's pid namespace scopes /proc to exactly the engine's own
# processes. Reads /proc directly — no ss/netstat dependency in the image.
#
# A snapshot can miss a transient connection; the launch scripts also set
# HF_HUB_OFFLINE / VLLM_NO_USAGE_STATS so the engine has no reason to dial
# out in the first place. This is the trust-but-verify half.
set -euo pipefail

AUDIT='
import glob, os, socket
def hexaddr(h):
    ip, port = h.split(":")
    b = bytes.fromhex(ip)
    if len(b) == 4:
        a = socket.inet_ntop(socket.AF_INET, b[::-1])
    else:
        g = b"".join(b[i:i+4][::-1] for i in range(0, 16, 4))
        a = socket.inet_ntop(socket.AF_INET6, g)
    return a, int(port, 16)
inode2sock = {}
for proto in ("tcp", "tcp6", "udp", "udp6"):
    try:
        lines = open(f"/proc/net/{proto}").read().splitlines()[1:]
    except FileNotFoundError:
        continue
    for ln in lines:
        f = ln.split()
        if f[3] != "01":  # established/connected only
            continue
        ra, rp = hexaddr(f[2])
        inode2sock[f[9]] = (proto, ra, rp)
def is_local(a):
    if a.startswith("::ffff:"): return is_local(a[7:])
    if a.startswith(("127.", "10.", "192.168.", "169.254.", "::1", "fe80", "fd")): return True
    if a.startswith("172."):
        try: return 16 <= int(a.split(".")[1]) <= 31
        except ValueError: return False
    return False
seen = set()
for fd in glob.glob("/proc/[0-9]*/fd/*"):
    try: t = os.readlink(fd)
    except OSError: continue
    if not t.startswith("socket:["): continue
    ino = t[8:-1]
    if ino not in inode2sock: continue
    pid = fd.split("/")[2]
    try: comm = open(f"/proc/{pid}/comm").read().strip()
    except OSError: comm = "?"
    seen.add((comm, pid) + inode2sock[ino])
ext = 0
for comm, pid, proto, ra, rp in sorted(seen):
    tag = "LOCAL" if is_local(ra) else "INTERNET"
    ext += tag == "INTERNET"
    print(f"    {tag:8s} {comm}[{pid}] {proto} -> {ra}:{rp}")
if not seen:
    print("    no established connections")
raise SystemExit(1 if ext else 0)
'

found=0 any=0
for c in serve_solo serve_node; do
  docker ps -q --filter "name=^${c}$" | grep -q . || continue
  any=1
  echo "  $c:"
  if ! docker exec "$c" python3 -c "$AUDIT"; then found=1; fi
done
[ "$any" = 1 ] || { echo "  nothing serving on this node"; exit 0; }
if [ "$found" = 1 ]; then
  echo "  VERDICT: INTERNET TRAFFIC PRESENT — see lines above"
else
  echo "  VERDICT: all connections local — nothing leaves the rack"
fi
exit $found
