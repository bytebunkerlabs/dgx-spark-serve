# How rack works — pull to death

> **Before 1.0.** This describes `rack` as it drove the two-Spark rack before
> dgx-serve 1.0: `rack up` held the foreground, one fixed worker, flat recipe
> files, `rack logs` reading a launch log. Since 1.0 engines run detached
> (`rack up` returns when healthy; `rack down` stops them), the inventory names
> the machines, recipes are a folder per model with a file per platform, and
> Macs and Windows serve too: [docs/12-platforms.md](12-platforms.md). What this
> page says about the fabric, NCCL, the images, memory and the recipe method
> still holds.

*The whole path: an org/model string on Hugging Face, to bytes on two disks, to
a container holding a model in unified memory, to a number in a JSONL file, to
nothing left running.*

*This is the narrative. For dgx-serve 1.0 on every platform (Spark, NVIDIA
Linux, Windows, Mac), the commands and their flags, see
[docs/12-platforms.md](12-platforms.md).*

---

## The shape of it

`rack` is one bash script. There is no daemon, no database, no state directory,
no controller. That is a design choice with a consequence you can lean on:
**every command is idempotent and everything it knows is on disk in three
places.**

```
.env          config that changes when the HARDWARE changes   (IPs, NICs, cache path, image tag)
recipes/*.env answers that change when the MODEL changes      (bash: MODEL, SERVE_ARGS, ENV_EXTRA, MODS)
scripts/*     the mechanics, identical for every model
```

`rack` itself is a dispatcher over `scripts/`. `rack up` is `launch-solo.sh` or
`launch-cluster.sh` depending on one thing it reads out of the recipe. `rack
pull` is a download plus `sync-model.sh`. Nothing is hidden in the tool.

One practical split, worth knowing before you type anything: **authoring
commands work from anywhere; lifecycle commands run on the head.** `fit`, `new`,
`recipes`, `models`, `status` reach the nodes over ssh. `pull`, `up`, `bench`,
`chat`, `logs`, `down` drive the local docker socket, so they belong on
`spark-1`.

---

## Where the bytes live

Three machines, three different roles:

```
Mac (laptop)          authoring only — the git repo, recipes you edit
  ~/Documents/AI/dgx-spark-serve

spark-1 (head)        the repo again + the model cache + the engine
  ~/dgx/dgx-spark-serve         checkout, origin = github
  ~/dgx/hf/                     HF_CACHE — the weights
  ~/.cache/vllm, ~/.triton      compiled kernel artifacts (see "why boots get faster")

spark-2 (worker)      a replica of both
  ~/dgx/dgx-spark-serve         checkout, origin = spark-1 over the fabric
  ~/dgx/hf/                     byte-identical copy of the cache
```

The cache path is **identical on both nodes** and that is load-bearing, not
tidiness. At TP=2 the same `vllm serve` command runs on both boxes and resolves
the same path inside the same mount. A different path on the worker is a
mid-load failure with an unhelpful message.

### The Hugging Face cache layout

This is HF's format, not ours, and it surprises people the first time:

```
~/dgx/hf/hub/models--Qwen--Qwen3-8B/
├── refs/main                     -> "b968826d9c46..."   the commit you have
├── snapshots/b968826d9c46.../    the human-shaped view: real filenames
│   ├── config.json               -> ../../blobs/d46195ac...
│   ├── model-00001-of-00005.safetensors -> ../../blobs/31d6a825...
│   └── model.safetensors.index.json     -> ../../blobs/2b85c00f...
├── blobs/                        the actual data, content-addressed
│   ├── 31d6a825...  3,996,250,744 bytes
│   └── 20c2d636...  1,244,659,840 bytes
├── trees/<sha>.json              hub metadata
└── .no_exist/<sha>/              negative cache: files known ABSENT
```

Every file in `snapshots/` is a **symlink** into `blobs/`. The names you
recognise live in the snapshot; the bytes live under their own hash. Two model
revisions sharing an unchanged tokenizer share one blob.

Two consequences that bite:

- **`du` on the snapshot lies** — follow symlinks or you'll measure a directory
  of pointers.
- **Copy with a tool that understands symlinks.** `sync-model.sh` uses
  `rsync -ah`, which preserves them; a naive copy either explodes the size or
  ships dangling links.

`MiniMax-H3` shows 12K in `hub/` for exactly this reason — its real 144 GB
partition was fetched to `~/dgx/hf/local/MiniMax-H3/FL2VA`, outside the hub
layout, and the recipe points at that path directly.

---

## `rack pull` — what actually happens

```bash
rack pull Qwen/Qwen3-4B
```

Four steps, and the middle one exists because of a bug that cost a run:

**1. Download, inside the image.** Not with a host `hf` CLI — with the serving
image itself:

```
docker run --rm -v ~/dgx/hf:/root/.cache/huggingface \
  --entrypoint bash $IMAGE -c "hf download 'Qwen/Qwen3-4B'"
```

The image is the only place the toolchain version is pinned, so downloading
through it means the thing that fetched the weights and the thing that will
load them agree.

**2. Hand the tree back.** The container runs as root, so everything it creates
is root-owned — and hub metadata under `trees/` lands mode 600, unreadable to
your user. The failure that produced this line: `rsync` died mid-flight with
exit 23, and because the script was `set -e`, **shard verification was skipped
entirely** — the worst part, since you were then told nothing rather than told
the copy was incomplete.

```
docker run --rm -v ~/dgx/hf:/cache --entrypoint chown $IMAGE \
  -R "$(id -u):$(id -g)" /cache/hub/models--Qwen--Qwen3-4B
```

**3. Replicate to the worker over the fabric.** `sync-model.sh`, with two
measured choices: `WORKER_SSH` resolves to the *fabric* IP via `/etc/hosts`, not
the management LAN, and the cipher is forced to `aes128-gcm` because it's
hardware-accelerated — the default ssh cipher becomes the bottleneck long before
a 100 Gb/s link does. Never run it under `sudo`: that runs ssh as root, and root
has no key.

**4. Verify on both nodes.** Not "did rsync exit 0" — parse
`model.safetensors.index.json`, take the set of shard filenames out of
`weight_map`, and check each one exists. On the head, then over ssh on the
worker. Incomplete caches fail at load time with far less obvious errors.

---

## What a recipe is, on disk

A recipe is **bash that gets sourced**, not a config file that gets parsed:

```bash
SERVE_ARGS=() ENV_EXTRA=() MODS=()
. "$RECIPE"
: "${MODEL:?recipe must set MODEL}"
```

That's why they're arrays. Arrays survive quoting, which matters the moment a
flag's value is JSON:

```bash
--compilation-config '{"cudagraph_mode":"PIECEWISE","custom_ops":["all"]}'
```

It also explains inheritance for free — `. recipes/dsv4.env` at the top of a
variant, then `SERVE_ARGS+=(--host 192.168.100.2)`. It's just sourcing. Last
flag wins, which is how a two-line variant overrides a bind address without
restating anything.

**`MODEL=` is either a hub id or a container path.** `Qwen/Qwen3-4B` is resolved
by the HF client *from the mounted cache* — and since `HF_HUB_OFFLINE=1` is
baked into the launchers, that resolution never touches the network. An absolute
path like `/root/.cache/huggingface/local/MiniMax-H3/FL2VA` is the escape hatch
for weights that don't live in hub layout. Both are "on disk"; only the lookup
differs.

---

## `rack up` — solo

One `docker run`, foreground, `--rm`:

```
docker run --rm --name serve_solo --network host --gpus all --ipc=host \
  --log-driver journald --ulimit nofile=1048576:1048576 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e VLLM_NO_USAGE_STATS=1 -e DO_NOT_TRACK=1 \
  <ENV_EXTRA> \
  -v ~/dgx/hf:/root/.cache/huggingface \
  -v ~/.cache/vllm:/root/.cache/vllm \
  -v ~/.cache/flashinfer:/root/.cache/flashinfer \
  -v ~/.triton:/root/.triton \
  $IMAGE vllm serve $MODEL --host 127.0.0.1 --port $API_PORT "${SERVE_ARGS[@]}"
```

Reading it flag by flag:

- **`--rm` + foreground** — Ctrl-C stops *and* cleans up. There is no "stopped
  container" state to garbage-collect.
- **`--log-driver journald`** — because `--rm` deletes the container and its
  logs with it. A crash trace has to outlive the thing that crashed:
  `journalctl CONTAINER_NAME=serve_solo --since "2 hours ago"`.
- **`--network host`** — the API lands on the box's real interfaces. Bind
  carefully; the recipe's last `--host` wins over the script's `127.0.0.1`.
- **The four offline vars** — set here, in the choke point every launch passes
  through, rather than per-recipe. A recipe that genuinely needs the hub can
  override in `ENV_EXTRA`, since a later `-e` wins.
- **The three cache mounts** — `vllm`, `flashinfer`, `triton`. This is why the
  second boot of a model is dramatically faster than the first: compiled kernels
  and graph artifacts persist on the host across container lifetimes.

Before the run, best-effort `drop_caches`. On unified memory, cached file pages
and GPU allocations come from the same pool, so a big load can OOM with `free`
showing room. Best-effort by design — it warns and continues without
passwordless sudo.

**Mods, solo flavor:** each mod directory has an `overlay/` tree mirroring the
container filesystem, and every file in it is bind-mounted read-only over its
counterpart in the image. Two-file patch, two `-v` flags, no rebuild.

---

## `rack up` — TP=2

Same recipe, different mechanics, because the model spans both boxes.

```
1. idle keep-alive container per node:  docker run -d --name serve_node ... sleep infinity
2. sanity gates
3. mods applied INSIDE both containers
4. worker launched first (detached), head second (foreground)
```

Why a keep-alive container instead of running the engine directly? PID 1 is
`sleep infinity`, and the real launch is a `docker exec` whose output is
redirected to `/proc/1/fd/1` — so everything lands in `docker logs` on both
nodes, including the worker, which has no API server to talk to.

The gates before launch are the interesting part, because each one encodes a
failure:

- **Am I the head?** Reads the fabric IP off `FABRIC_IF` and compares to
  `HEAD_IP`. Cluster launches only work from `spark-1`.
- **Do both nodes have the *same image*?** Compares `docker image inspect --format '{{.Id}}'`
  locally and over ssh. Different builds on the two nodes is a subtle
  cross-node failure; this makes it a loud one.
- **Does this vLLM even have native multi-node?** Probes
  `EngineArgs.nnodes` from inside the container — *not* `--help`, because 0.26.0
  accepts `--nnodes` while hiding it from help output. Fails before launch
  rather than during.

Then the launch itself. Both nodes run the **same generated script** — identical
`vllm serve $MODEL` plus identical `SERVE_ARGS` — differing only in four
topology flags:

```
--nnodes 2 --node-rank {0|1} --master-addr $HEAD_IP --master-port 29501
                                                    (+ --headless on rank 1)
```

Worker first, detached; head second, foreground. Worker-first avoids a
rendezvous race. This is vLLM's native torch-distributed path — **no Ray**, and
never pass `--distributed-executor-backend`.

The per-node env is where the Spark-specific knowledge sits: `VLLM_HOST_IP` per
node, `NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`/`TP_SOCKET_IFNAME` pinned to the
fabric NIC, and `NCCL_IB_HCA` naming **both RoCE twins** of the single cabled
port — one alone caps NCCL at about half the link. `NCCL_IB_GID_INDEX` is
deliberately never set: NCCL ≥ 2.21 picks the RoCEv2/IPv4 GID itself, and a
stored index rots when the kernel renumbers the table.

A `trap cleanup INT TERM EXIT` stops both containers on any exit path, so a
Ctrl-C on the head doesn't strand a worker holding 85 GB.

---

## Serving, measuring, dying

**Who answers.** Solo binds `127.0.0.1:$API_PORT` unless the recipe overrides;
TP=2's head serves the API and the worker is `--headless`. LiteLLM fronts it on
:4000 and is what the console and other clients actually talk to.

**`rack chat` / `rack bench` don't take a model name.** They ask `/v1/models`
and use whatever is serving. If nothing answers, they say so rather than
guessing — which is the same principle as `status` saying UNKNOWN.

**`rack bench`** runs a warmup, then N timed streaming completions, records TTFT
and decode tok/s per run, prints the median, and appends **one JSON line per run
to `bench/results.jsonl`** with the git rev. That file is the point: it's how a
tuning change three weeks from now has something to be compared against.

**`rack down`** is `docker stop serve_node serve_solo` on both nodes. Because
everything runs `--rm`, **stop is cleanup** — no dangling containers, no state
to reconcile. What survives on purpose: the journald logs, the compiled kernel
caches, the weights, and `results.jsonl`.

---

## The full trace

```
rack fit Qwen/Qwen3-4B        HF API → arithmetic → verdict. No bytes moved.
rack new probe-4b …           recipes/probe-4b.env from TEMPLATE.env. Local file.
                              → you answer the eight questions
rack pull Qwen/Qwen3-4B       image downloads → chown → rsync to worker → verify BOTH
                              → ~/dgx/hf/hub/models--Qwen--Qwen3-4B on two disks
rack up probe-4b              is_tp2? → launch-solo.sh
                              drop_caches → docker run → weights read from the mount
                              → kernels compiled into ~/.cache/vllm (first boot only)
                              → API on 127.0.0.1:8888
rack status                   ssh to head: docker ps, free -h, /v1/models
rack chat "…"                 GET /v1/models → POST /v1/chat/completions, streamed
rack bench probe-4b           warmup + N runs → median → append bench/results.jsonl
rack down                     docker stop → --rm removes → journald keeps the log
```

Nothing left resident. The four things that persist are the weights, the
compiled kernels, the measurements, and the recipe — which is to say, everything
that took time to produce, and nothing that didn't.
