# 11 — The monitor: one endpoint for the whole rack

*`rack monitor up` puts a small read-only telemetry service on every node and
prints one URL. Add that URL and its token to the ByteBunker app (Cluster >
Add monitor) and the app shows every node, GPU, engine and container from it.*

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

Two containers per node, one image (`rack-monitor:<hash>`, python:3.12-slim
plus `monitor/rackmon.py`, standard library only):

| container | sees | cannot |
|---|---|---|
| `rack-monitor` | host network, host pids, host cgroups (all read-only); the GPU through the NVIDIA runtime with `utility` capability only | write anything (read-only root, no capabilities, your uid, `no-new-privileges`, 256 MB, 64 pids); touch Docker |
| `rack-monitor-docker` | the Docker socket | reach any network (`--network none`); it only ever calls `GET /containers/json` and writes the slimmed list to `~/.local/state/rack-monitor/containers.json` |

The split is the point: the process that answers the network never holds the
Docker socket (which is root on the node). The one that holds it has no
network to be reached through.

## Commands

```bash
rack monitor up          # build, ship to the worker, (re)start both nodes, print the endpoint
rack monitor status      # containers, one line per node, the endpoint again
rack monitor token       # print the token (rack monitor token --rotate: replace it)
rack monitor logs        # the head's monitor log (logs worker: the worker's)
rack monitor down        # stop and remove on both nodes; the token stays
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

| path | state | why |
|---|---|---|
| tailnet `http://100.90.164.11:9177`, `http://burhan.tailed338.ts.net:9177` | open | ufw allows `tailscale0` |
| fabric `192.168.100.x:9177` | open | how the head reaches the worker (the worker listens nowhere else) |
| LAN `http://172.16.25.186:9177` | closed | ufw drops it. LiteLLM's :4000 is open on the LAN only because Docker-published ports bypass ufw; a host-network service does not |

`rack monitor up` tests the LAN path from the worker and prints the exact rule
when it is closed. Opening it is your call (it needs sudo):

```bash
sudo ufw allow from 172.16.25.0/24 to any port 9177 proto tcp
```

## Configuration

`.env` (all optional):

| variable | default | meaning |
|---|---|---|
| `MONITOR_PORT` | 9177 | port on every node |
| `MONITOR_BIND` | 0.0.0.0 | the head's listen addresses, comma-separated |
| `MONITOR_CLUSTER` | rack | the name the app shows |
| `MONITOR_ENGINE_PORTS` | 8888,8000,8001,8002,8080,30000,11434,1234 | where engines may listen |
| `WORKER_IP` | 192.168.100.2 | the worker's fabric address, which the head polls |

Inside the container (set by `rack monitor up`): `MONITOR_NAME`,
`MONITOR_ROLE`, `MONITOR_PEERS` (`name=http://ip:port,...`), `MONITOR_SAMPLE_S`
(2), `MONITOR_TOKEN_FILE`, `MONITOR_STATE_DIR`.

## Running it somewhere else

`rackmon.py` is one file with no dependencies, so any Linux box can run it
bare (the agents worker, a gaming PC under WSL2):

```bash
MONITOR_TOKEN_FILE=~/.config/rack/monitor.token MONITOR_NAME=leagueofash \
  python3 monitor/rackmon.py serve
```

Without the relay it reports no containers and says so. Add it to the head's
peers with `MONITOR_PEERS` or as its own monitor in the app.

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
| no engines listed while one serves | port not in `MONITOR_ENGINE_PORTS` | add it in `.env`, `rack monitor up` |
| app says unauthorized | token pasted with a newline or an old token | `rack monitor token`, paste again |
