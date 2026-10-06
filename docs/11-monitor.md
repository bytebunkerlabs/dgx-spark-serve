# 11 — The monitor: one endpoint for the whole rack

*`rack monitor up` puts a small read-only telemetry service on every node and
prints one URL. Add that URL and its token to the ByteBunker app (Cluster >
Add monitor) and the app shows every node, GPU, engine and container from it.
On a DGX Spark or NVIDIA Linux it runs in containers on the head and on every
worker of the inventory; on a Mac or in WSL2 it runs bare, as your user.*

---

## What runs where

```
 head (spark-1)                                   worker (spark-2)
 ┌──────────────────────────────────────┐         ┌──────────────────────────────┐
 │ rack-monitor         :9177 (token)   │ fabric  │ rack-monitor     :9177       │
 │   samples this node every 2 s        │────────▶│   samples this node          │
 │   /v1/cluster = itself + the worker  │ GET     │   /v1/node                   │
 │ rack-monitor-docker  (no network)    │ /v1/node│ rack-monitor-docker          │
 │   docker.sock → containers.json      │         │   docker.sock → file         │
 └──────────────────────────────────────┘         └──────────────────────────────┘
          ▲  http://<head>:9177 + token  (tailnet; LAN if you open it)
          │
   ByteBunker app / console  ── Cluster screen
```

Every worker of the inventory (`rack nodes`) gets its own, and the head lists
them as peers by fabric address. Two containers per node, one image
(`rack-monitor:<hash>`, python:3.12-slim plus `monitor/rackmon.py`, standard
library only):

| container | sees | cannot |
|---|---|---|
| `rack-monitor` | host network, host pids, host cgroups (all read-only); the GPU through the NVIDIA runtime with `utility` capability only | write anything (read-only root, no capabilities, your uid, `no-new-privileges`, 256 MB, 64 pids); touch Docker |
| `rack-monitor-docker` | the Docker socket | reach any network (`--network none`); it only ever calls `GET /containers/json` and writes the slimmed list to `~/.local/state/rack-monitor/containers.json` |

The split is the point: the process that answers the network never holds the
Docker socket (which is root on the node). The one that holds it has no
network to be reached through.

## Bare: a Mac, WSL2, or `--bare`

Where Docker cannot run the monitor (a Mac; a machine without a usable Docker)
or when you ask (`rack monitor up --bare`), `rackmon.py` runs as your user
straight from the checkout: a launchd agent on a Mac
(`ai.bytebunker.dgx-serve.monitor`), a systemd user unit elsewhere
(`rack-monitor.service`, plus `rack-monitor-relay.service` for the container
list when Docker is usable). Same token, same port, same API, and it restarts
with the machine (on Linux, with linger: `sudo loginctl enable-linger $USER`).
A bare monitor runs as the engine's own user, so it may read the engine key for
a keyed engine's metrics (`MONITOR_ENGINE_KEY_FILE`); it sends the key only to
the local engine.

On a Mac it reads, without root: per-core CPU from the Mach host statistics,
memory from `vm_stat` (available = free, inactive, speculative and purgeable
pages) and `vm.swapusage`, the GPU's own utilisation and memory in use from the
IORegistry (`IOAccelerator`), throttling from `pmset -g therm`, listening ports
and interface counters from `netstat`, uptime from `kern.boottime`. A Mac gives
no per-sensor temperatures without root, so none are reported.

## Commands

```bash
rack monitor up [--bare] # build, ship to every worker, (re)start, print the endpoint
rack monitor status      # containers or service, one line per node, the endpoint again
rack monitor token       # print the token (rack monitor token --rotate: replace it)
rack monitor logs [<w>]  # the head's monitor log, or a worker's
rack monitor down        # stop and remove everywhere; the token stays
```

`rack monitor up` is idempotent. The image tag is a hash of `rackmon.py` and
the Dockerfile, so editing the monitor and running `up` again rebuilds, ships
and restarts; running it unchanged just restarts. Both containers carry
`--restart unless-stopped`, so they come back after a reboot without `rack`.

`rack status` ends with a one-line-per-node monitor summary.

## What it measures

Every field is measured on the node it describes. A number the node cannot
measure is `null`, never a guess (the GB10 reports no GPU memory total, for
instance, because its memory is the system's: the monitor reports system
memory from the kernel and per-process GPU allocations from nvidia-smi).

| area | source | fields |
|---|---|---|
| system | DMI, `/proc/cpuinfo`, host `os-release`, `uname` | product (DGX Spark), core mix (10x Cortex-X925 + 10x Cortex-A725), OS, kernel, uptime |
| CPU | `/proc/stat`, `/proc/loadavg` | busy %, every core's busy %, load 1/5/15 |
| memory | `/proc/meminfo` | total, used, available, cached, swap; `gpu_procs` = what GPU processes hold |
| GPU | `nvidia-smi` (~30 ms) | utilization, temperature, power, SM clock vs max, P-state, throttle reasons, driver, processes with their memory and container |
| temperatures | `/sys/class/hwmon` | GPU, SoC (ACPI zones), NVMe, NIC (ConnectX) |
| network | `/proc/net/dev`, sysfs | per interface: kind (fabric = ConnectX, lan, tailnet, wifi), IPv4, link speed, rx/tx bytes per second |
| disk | `statvfs` | root filesystem used / total |
| engines | each listening port in `MONITOR_ENGINE_PORTS` (default 8888, 8000, 8001, 8002, 8080, 30000, 11434, 1234) | vLLM, SGLang, llama.cpp from `/metrics`; Ollama from `/api/ps`; anything else OpenAI-shaped from `/v1/models`. Running / waiting requests, KV cache %, KV capacity in tokens, generation and prompt tokens per second (10 s window), TTFT p50 / p95, end-to-end latency and decode-step latency, prefix-cache hit rate (60 s window), preemptions, served model ids and context length. With speculative decoding (MTP, EAGLE, n-gram) also tokens per decode step and the share of drafted tokens kept: one step can emit several tokens, so step latency is not per-token latency |
| containers | the relay's file + cgroup v2 | name, image, state, status, compose project, CPU cores in use, memory |
| serving | `rack up`'s record (`~/.local/state/dgx-serve/serving.json`) | recipe, model, served name, its route on a gateway, engine, runtime, port, nodes, roles, dialect, context, tools, reasoning, vision, speculative decoding, whether a key is required, since when. The engine on that port is found even when its probes need the key |
| history | ring buffer in the monitor | 15 minutes at 2 s: CPU, memory, GPU, temperature, power, tokens/s, KV %, fabric / LAN / tailnet bytes |

Only listening ports on that list are probed, and only with `GET`: the
monitor never connects to NCCL, torch or Ray ports. Command lines never
leave the node (they carry API keys often enough); a GPU process is labelled
by engine family (`vllm`) or program name. The relay drops container env,
command and labels for the same reason.

## API

```
GET /v1/hello                  no token: {"service": "rack-monitor", "name": "spark-1", ...}
GET /v1/node?history=N         this node; N = samples of history (0..450)
GET /v1/cluster?history=N      the head plus every peer, one request
```

Authentication is one token: `Authorization: Bearer <token>`, or HTTP Basic
with any user name and the token as the password, which lets a browser or curl
open it directly:

```bash
curl -s -H "Authorization: Bearer $(cat ~/.config/rack/monitor.token)" \
  http://127.0.0.1:9177/v1/cluster | python3 -m json.tool | less
```

Everything else is `405 read-only`. The token lives in
`~/.config/rack/monitor.token` (0600) on every node; the head presents it when
it asks the worker.

## Reaching it

The head's monitor binds `0.0.0.0:9177` on the host network (`MONITOR_BIND`
to narrow it). The worker's binds only its fabric address and loopback: the head
is the only thing that asks it. On this rack that means:

| path | state on a Spark with ufw | why |
|---|---|---|
| tailnet `http://<head's tailnet address>:9177` | open | ufw allows `tailscale0` |
| fabric `<worker's fabric address>:9177` | open | how the head reaches a worker (a worker listens nowhere else) |
| LAN `http://<head's LAN address>:9177` | closed | ufw drops it. A gateway on :4000 may be open on the LAN only because Docker-published ports bypass ufw; a host-network service does not |

`rack monitor up` tests the LAN path from the worker and prints the exact rule
when it is closed. Opening it is your call (it needs sudo):

```bash
sudo ufw allow from 192.168.1.0/24 to any port 9177 proto tcp    # your LAN
```

## Configuration

`.env` (all optional):

| variable | default | meaning |
|---|---|---|
| `MONITOR_PORT` | 9177 | port on every node |
| `MONITOR_BIND` | 0.0.0.0 | the head's listen addresses, comma-separated |
| `MONITOR_CLUSTER` | rack | the name the app shows |
| `MONITOR_ENGINE_PORTS` | 8888,8000,8001,8002,8080,30000,11434,1234 | where engines may listen |
| `MONITOR_WAIT_S` | 30 | how long `up` waits for the first samples |

The workers and their fabric addresses come from the inventory (`rack nodes`).

Inside the container or the bare service (set by `rack monitor up`):
`MONITOR_NAME`, `MONITOR_ROLE`, `MONITOR_PEERS` (`name=http://ip:port,...`),
`MONITOR_SAMPLE_S` (2), `MONITOR_TOKEN_FILE`, `MONITOR_STATE_DIR`,
`MONITOR_SERVING` (rack up's record), and bare only `MONITOR_ENGINE_KEY_FILE`.

## Running it by hand

`rackmon.py` is one file with no dependencies, so it also runs by hand on any
Linux box or Mac:

```bash
MONITOR_TOKEN_FILE=~/.config/rack/monitor.token MONITOR_NAME=pc python3 monitor/rackmon.py serve
python3 monitor/rackmon.py once        # one snapshot of this machine, pretty
```

Without the relay it reports no containers and says so. Add it to the app as
its own monitor.

## Debugging

```bash
docker run --rm --network host --pid host --gpus all rack-monitor:latest once   # one snapshot, pretty
rack monitor logs
python3 -m unittest monitor/test_rackmon.py -v                                  # parsers, auth, peers, relay
```

| symptom | cause | fix |
|---|---|---|
| `containers_error: docker relay not running` | `rack-monitor-docker` stopped | `rack monitor up` |
| worker shows `peer answered HTTP 401 (token mismatch)` | tokens differ | `rack monitor up` copies the head's token to the worker |
| worker shows `timed out` | worker monitor down, or the fabric is | `rack monitor status`; `rack preflight` |
| no engines listed while one serves | port not in `MONITOR_ENGINE_PORTS`, and not rack up's | add it in `.env`, `rack monitor up` |
| bare: `not running` after a reboot (Linux) | no linger | `sudo loginctl enable-linger $USER` |
| app says unauthorized | token pasted with a newline or an old token | `rack monitor token`, paste again |
