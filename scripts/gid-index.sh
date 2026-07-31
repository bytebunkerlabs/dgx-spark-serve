#!/usr/bin/env bash
# Print the RoCE v2 GID index whose GID embeds this device's own IPv4.
#
# Never store this number. The kernel renumbers the GID table whenever an
# interface bounces — a `netplan apply` is enough. A running service keeps
# serving on already-established queue pairs for hours after its configured
# index stops existing, then fails only on the next restart with:
#   ibv_modify_qp failed with 61 No data available
# Resolve it from hardware immediately before every launch, use it, forget it.
set -eu
dev=${1:-rocep1s0f0}
ndev=$(cat "/sys/class/infiniband/$dev/ports/1/gid_attrs/ndevs/0")
ip=$(ip -4 -br addr show "$ndev" | awk '{print $3}' | cut -d/ -f1)
[ -n "$ip" ] || { echo "gid-index: no IPv4 on $ndev" >&2; exit 1; }
# 192.168.100.1 appears in the GID as ...ffff:c0a8:6401
# shellcheck disable=SC2086
tail=$(printf '%02x%02x:%02x%02x' ${ip//./ })
for i in $(seq 0 15); do
  [ "$(cat "/sys/class/infiniband/$dev/ports/1/gid_attrs/types/$i" 2>/dev/null)" = "RoCE v2" ] || continue
  case "$(cat "/sys/class/infiniband/$dev/ports/1/gids/$i")" in
    *"ffff:$tail") echo "$i"; exit 0 ;;
  esac
done
echo "gid-index: no RoCE v2 GID matching $ip on $dev" >&2
exit 1
