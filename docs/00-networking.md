# 00 — The fabric: what preflight checks and why

The full bring-up story is in the build log
([blog.bytebunkerlabs.ai — two DGX Sparks, one fabric](https://blog.bytebunkerlabs.ai/posts/dgx-spark-cluster-build-log/))
and the host modules live in
[bytebunkerlabs/dgx-spark-setup](https://github.com/bytebunkerlabs/dgx-spark-setup).
This doc is the operational summary: every check in `scripts/preflight.sh`, and the failure it
exists to catch. If preflight is all-PASS on both nodes, everything below is true.

## One port, two rails

The Spark's ConnectX-7 uses Socket Direct: one physical QSFP port fed by **two independent PCIe
Gen5 ×4 paths**, presented by Linux as two interfaces (`enp1s0f0np0`, `enP2p1s0f0np0`) and two
RDMA devices. They share `phys_switch_id` — one ASIC, two doors. Each rail line-rates at
~109 Gb/s (PCIe ×4 ceiling); both together measured **196.03 Gb/s**, 98% of the port.

Configure only the first interface and you run on half the fabric. Every tutorial does exactly
that. Addressing: rail 1 `192.168.100.x/24`, rail 2 `192.168.101.x/24` (**never the same subnet
on both twins** — it scrambles routing), MTU 9000, **no gateway on either** — the default route
stays on the management NIC.

For NCCL the split of duties is: TCP control plane on ONE Ethernet twin
(`NCCL_SOCKET_IFNAME=enp1s0f0np0`), RDMA data plane on **BOTH** RoCE twins
(`NCCL_IB_HCA=rocep1s0f0,roceP2p1s0f0`). One HCA alone caps NCCL at one PCIe rail (~100 Gb/s);
both is the single most Spark-specific line in the whole stack. The launch scripts set this
from `IB_HCAS` in `.env`.

Caveat that keeps this honest: for decode-heavy serving, doubling the fabric moved end-to-end
inference by **0.15%** — decode all-reduces are tens of kilobytes and latency-bound, not
bandwidth-bound. The second rail matters for weight staging (rsync) and prefill-heavy loads,
not for tok/s.

## The GID index rots — so we don't use one at all

`NCCL_IB_GID_INDEX` names an entry in a kernel table that gets **renumbered whenever an
interface bounces** — a `netplan apply` is enough. The vicious part: established queue pairs
keep working, so a stale index serves happily for hours and fails only at the next restart:

```
NCCL WARN Call to ibv_modify_qp failed with 61 No data available
```

`61` is ENODATA — the table entry you configured no longer exists. Hardware is fine; do not
debug cables.

The old outage came from a toolkit that *hardcoded* the index. The verified modern answer is
better than resolving it fresh: **since NCCL 2.21, leaving `NCCL_IB_GID_INDEX` unset makes NCCL
select the RoCEv2 + IPv4 GID itself** (`NCCL_IB_ROCE_VERSION_NUM` defaults to 2,
`NCCL_IB_ADDR_FAMILY` to AF_INET). Every Spark playbook — NVIDIA's and the community's — leaves
it unset. So do our launch scripts, deliberately. If you ever must constrain, constrain by
*meaning* (`NCCL_IB_ADDR_RANGE=192.168.100.0/24`), never by table position.

`scripts/gid-index.sh` stays in the toolbox as a **diagnostic** (preflight uses it to prove a
RoCE v2 GID exists for the interface IP) and for third-party stacks that still demand an index.
Related hygiene: `dhcp6: no` + `link-local: []` in netplan keep stray IPv6/169.254 addresses
from bloating the GID table in the first place.

## The firewall passes ping and eats RDMA

`ufw` rules are per-interface. A rule for rail 1 does nothing for rail 2, and ICMP can pass
while the RDMA handshake is silently dropped — link pings, `ib_write_bw` hangs forever. Both
fabric interfaces need their own `allow in on <iface>`. Preflight checks both.

## Memory is one pool and the cache is part of it

128 GB unified: `nvidia-smi` reports memory as "Not Supported" — `free -h` is the truth.
Two standing rules before any big load:

- **Drop the page cache**: `sync && sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'` — after
  serving or copying weights, the cache holds tens of GB and the next load OOMs "with memory
  free".
- **earlyoom off for cluster runs** — it kills a vLLM worker on transient pressure mid-load.

## Reading NCCL logs

Two lines decide whether a cluster launch is real:

| log line | meaning |
|---|---|
| `NET/IB` | NCCL is on RDMA — correct |
| `NET/Socket` | silent TCP fallback — every number after this is invalid; stop and fix |

Also benign but alarming: `No available shared memory broadcast block found in 60 seconds`
during startup means one rank is compiling kernels while the other waits.

## Sign-off numbers (measured, 2026-07)

| measurement | value |
|---|---|
| link | 200 GbE negotiated, MTU 9000 |
| RTT | ~1 ms, 0% loss |
| TCP (iperf3, 4 streams) | 111 Gb/s, 0 retransmits |
| RoCEv2 single QP, one rail | 109.22 Gb/s |
| RoCEv2 both rails | 196.03 Gb/s |
