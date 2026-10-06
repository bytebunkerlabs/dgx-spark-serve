# 07 — The operator's manual

> **Before 1.0.** This describes `rack` as it drove the two-Spark rack before
> dgx-serve 1.0: `rack up` held the foreground, one fixed worker, flat recipe
> files, `rack logs` reading a launch log. Since 1.0 engines run detached
> (`rack up` returns when healthy; `rack down` stops them), the inventory names
> the machines, recipes are a folder per model with a file per platform, and
> Macs and Windows serve too: [docs/12-platforms.md](12-platforms.md). What this
> page says about the fabric, NCCL, the images, memory and the recipe method
> still holds.

*Everything, end to end: every variable, every knob, what it does, how to change
it, what it looks like when it's wrong, and how to recover — written so you can
run this rack alone at 1 a.m. with nothing but this file. docs/06 is the
narrative version; this is the reference. Line numbers cite the repo as of
2026-08-08.*

---

## Part 1 — The map

### Three machines

```
Mac (laptop)     authoring: edit recipes, read results, drive status checks
spark-1 (head)   the engine: docker, weights, the API, every launch
spark-2 (worker) the second half of TP=2, and a second solo engine for video
```

Repo checkouts: Mac `~/Documents/AI/dgx-spark-serve` (origin = github),
spark-1 `~/dgx/dgx-spark-serve` (origin = github), spark-2 `~/dgx/dgx-spark-serve`
(origin = **spark-1 over the fabric** — plain `git pull` works there).
The worker path is **load-bearing**: `rack preflight/scrub/net` ssh in and
`cd dgx/dgx-spark-serve` — a moved checkout makes the worker half of those
commands print a lone `cd: ... No such file or directory` line and no results,
and rack carries on as if fine (the `|| true` swallows the failed exit status,
not the message).

### Which commands run where

| From anywhere (ssh to the nodes) | Head only (local docker/API) |
|---|---|
| `fit`, `new`, `recipes`, `models`, `status` | `pull`, `verify`, `up`, `down`, `build` |
| `preflight`, `scrub`, `net` (head half runs locally — see sharp edge S3) | `logs`, `bench`, `chat` |

The dividing line: anything that needs the docker socket or `127.0.0.1:8888`
runs where those exist. `status` and `models` were taught to ssh to the head
(2026-08-08); `logs`, `bench`, `chat`, `verify` were not — run those ON spark-1.

### The three config layers

| Layer | File | Changes when |
|---|---|---|
| launcher + `.env` | `scripts/launch-*.sh`, `.env` | the hardware changes |
| recipe | `recipes/<name>.env` | the model changes |
| invocation | command line | the run changes |

**Precedence, exactly:** script default `${VAR:-default}` ← exported
environment ← `.env` (plain assignments, sourced at script startup — **they
overwrite anything you exported**; an export only wins for a var `.env` leaves
unset) ← recipe (sourced after `.env` by the launchers, so recipe wins) ← last
flag on the command line (vLLM: last flag wins; docker `-e`: later wins). So
`IMAGE=test:tag rack up foo` works today (no `.env` exists) but silently stops
working the day you pin `IMAGE` in `.env`. Which scripts read `.env`: rack,
both launchers, preflight, stop-cluster, sync-model, build. **netcheck and
scrub read no config at all** — their container names and ports are literals.
This chain is why a two-line variant recipe works: it sources its parent and
appends one overriding flag.

**There is currently no `.env` on either node.** Every value below is running
on its default. To pin one: `cp .env.example .env` and edit — on **both** nodes
(each node's scripts read their own copy).

---

## Part 2 — Every configuration variable

### `.env` / environment (read by rack, the launchers, preflight, stop-cluster, sync-model, build — not netcheck/scrub)

| Var | Default | Used by | What it does | If wrong |
|---|---|---|---|---|
| `HEAD_IP` | `192.168.100.1` | rack, launch-cluster, preflight | Fabric IP of the head. rack uses it to detect "am I on the head"; launch-cluster refuses to run on a box that doesn't own it; it becomes `--master-addr` (the torch rendezvous address) and the head's `VLLM_HOST_IP` | Cluster: `this box is <ip>, not head <HEAD_IP> — run on spark-1`. rack: head/laptop misdetection — laptop commands query the laptop's docker |
| `WORKER_IP` | `192.168.100.2` | launch-cluster, preflight | Worker's fabric IP → worker's `VLLM_HOST_IP`; preflight's peer-ping target | Worker advertises wrong IP to the distributed engine; rendezvous hangs |
| `HEAD_SSH` | `spark-1` | rack | How a laptop reaches the head (`run_on_head`) | `status`/`models` print `UNKNOWN — could not …` |
| `WORKER_SSH` | `spark-2` | rack, launch-cluster, stop-cluster, sync-model, build | ssh alias for the worker. **Must resolve to the fabric IP via /etc/hosts**, not the tailnet/LAN — replication rides this. NOT used by preflight, which checks the peer by raw fabric IP — **an all-PASS preflight does not validate this alias** | Transfers silently route over the slow link; or `no passwordless ssh to <WORKER_SSH>` |
| `FABRIC_IF` | `enp1s0f0np0` | launch-cluster, preflight | Rail-1 netdev. Becomes `NCCL_SOCKET_IFNAME`, `GLOO_SOCKET_IFNAME`, `TP_SOCKET_IFNAME`, `UCX_NET_DEVICES` — every TCP control plane pinned to the fabric | Head-gate fails with empty IP; or control plane rides the management NIC |
| `FABRIC_IF2` | `enP2p1s0f0np0` | preflight | Rail-2 netdev (same QSFP port, second PCIe path) — checked, not used by launches | Preflight can't see half the fabric |
| `RDMA_DEV` | `rocep1s0f0` | preflight, gid-index | RDMA device for the GID diagnostic | `no RoCE v2 GID matches` FAIL |
| `IB_HCAS` | `rocep1s0f0,roceP2p1s0f0` | launch-cluster | → `NCCL_IB_HCA`. **Both RoCE twins, always.** The single most Spark-specific value in the stack | One twin only: NCCL silently caps at ~100 Gb/s (one PCIe rail). No error — just a plateau |
| `MASTER_PORT` | `29501` | launch-cluster | torch-distributed rendezvous port on the head | Occupied/blocked → rendezvous hang, no message |
| `HF_CACHE` | `$HOME/dgx/hf` | everything | Weights root, mounted at `/root/.cache/huggingface`. **Identical path on both nodes** — the same serve command must resolve it on each | `models: nothing cached`; offline load failure (engines run `HF_HUB_OFFLINE=1`, disk is the only source) |
| `IMAGE` | `dgx-spark-serve:dev` | rack pull, launchers, build | The engine image (recipes may override per-model) | pull: `image … missing — run: rack build`; cluster: `image differs between nodes` |
| `API_PORT` | `8888` | rack, launch-solo | The API port rack polls and solo binds. **Cluster ignores it** — see sharp edge S1 | status/bench/chat say nothing serving while the engine is up on another port |
| `USABLE_GB` | `110` | rack fit | Per-node usable memory in the fit arithmetic (`budget = USABLE_GB × nodes − 20`). On a discrete-GPU site: total usable VRAM across the box's GPUs | fit verdicts lie in either direction |
| `TOPOLOGY` | `auto` | rack up | `auto`: TP=2 recipes → cluster launcher (the Spark rack). `solo`: everything through launch-solo — a rented multi-GPU box, where TP means N GPUs in one machine (Part 11). `cluster`: force the two-node path | `TOPOLOGY must be auto, solo, or cluster` |
| `TEMPLATE` | `recipes/TEMPLATE.env` | rack | What `rack new` scaffolds from; excluded from listing/launch | `new` dies `missing <TEMPLATE>` |

### Hardcoded values worth knowing (edit the file to change)

| Value | Where | What |
|---|---|---|
| `20` GB | rack `cmd_fit` heredoc | KV/runtime reserve subtracted from the fit budget (flat, not per node) |
| `serve_solo` / `serve_node` | launch-solo:41, launch-cluster:27, stop-cluster:7-8, netcheck:67, rack status/logs | Container names. stop-cluster and netcheck carry them as **literals** — rename in one place and the others silently miss (stop still prints "stopped") |
| `4` | rack `is_tp2`/`recipe_model` | Max recipe-inheritance depth. Deeper: `is_tp2` silently reads solo; `recipe_model` prints `(inheritance loop)` |
| `5`s / `600`s / `1024` | rack | API-poll curl timeout / chat stream timeout / chat max_tokens |
| `60` | rack `cmd_logs` | Log tail window |
| `aes128-gcm@openssh.com` | sync-model:22, build:42 | Bulk-transfer cipher — hardware-accelerated; the default cipher bottlenecks before the link does |
| `100`/`20` GiB, `200` GB, `9000` | preflight | Memory PASS/warn thresholds, disk threshold, expected MTU |
| `dgx/dgx-spark-serve` | rack:262,373,382 | Worker repo path for preflight/scrub/net (relative to worker `$HOME`) |
| `1048576` | both launchers | `--ulimit nofile` soft:hard |

### The container environment (set by launchers, not by you)

Baked into every launch at the choke point, overridable per-recipe via
`ENV_EXTRA` (later `-e` wins):

| Var | Value | Why |
|---|---|---|
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` | weights come from the mount; the hub client never phones home for metadata |
| `VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK` | `1` | no telemetry to stats.vllm.ai. Verify anytime: `rack net` |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` | allocator behavior on unified memory |
| cluster only: `VLLM_HOST_IP` | per node | each node's fabric IP |
| cluster only: `NCCL_SOCKET_IFNAME` etc. | `$FABRIC_IF` | all four transport-pinning vars |
| cluster only: `NCCL_IB_HCA` | `$IB_HCAS` | both rails |
| cluster only: `NCCL_IB_DISABLE=0`, `NCCL_IGNORE_CPU_AFFINITY=1` | | force verbs on; scheduling |

**Deliberately absent, forever: `NCCL_IB_GID_INDEX`.** The kernel renumbers the
GID table whenever an interface bounces (`netplan apply` is enough). Established
queue pairs keep serving for hours on a stale index, then the *next restart*
dies with `ibv_modify_qp failed with 61 No data available`. ENODATA — the table
entry is gone. Hardware is fine; do not debug cables. NCCL ≥ 2.21 auto-selects
when unset. Must constrain? By meaning — `NCCL_IB_ADDR_RANGE=192.168.100.0/24` —
never by table position.

---

## Part 3 — The lifecycle, command by command

### `rack fit <org/model>` — arithmetic, no bytes moved

Two HTTPS GETs to huggingface.co (60 s timeout): the model API with
`?blobs=true` (sums `siblings[].size` → disk GB, counts `.safetensors` shards,
reads `safetensors.total` → params and effective bits-per-weight) and raw
`config.json` (MoE geometry → active params/token; `max_position_embeddings` →
context). Verdict per node count: `gb <= USABLE_GB × nodes − 20`.

**Interpretation rules:** the number it checks is *bytes on disk* — if your plan
involves load-time quantization (H3), fit is answered against the *quantized*
resident size and the tool's verdict is the wrong one; do that arithmetic by
hand and write it in the recipe. The 20 GB reserve is for KV + runtime; a
1M-context plan eats far more KV than 20 GB unless the KV is fp8 (dsv4: 1.29M
tokens fit because fp8 MLA KV is compact — measured, recipe comment).

### `rack new <name> <org/model>` — scaffold

Writes `recipes/<name>.env`: your header + `TEMPLATE.env` from the `# --- 1 FIT`
line down, with `MODEL`, `--served-model-name`, `--port` filled by full-line-
anchored sed. Refuses if the recipe exists or the template is missing. The two
sizing flags scaffold as `FILL_ME` — `rack up` refuses a recipe that still has
them, so an unfinished scaffold cannot reach the engine as a mangled command.

### `rack pull <org/model>` — download, hand back, replicate, verify

1. **Gate:** `docker image inspect $IMAGE` — dies `image … missing — run: rack build`.
2. **Download inside the image:** `docker run --rm -v $HF_CACHE:/root/.cache/huggingface
   --entrypoint bash $IMAGE -c "hf download '<id>'"`. The image is where the
   toolchain is pinned — downloader and loader always agree.
3. **chown back:** the container runs as root and writes hub metadata mode 600
   (`trees/*.json`). Measured failure: rsync died exit 23 mid-replication AND
   `set -e` then skipped shard verification — you were told nothing. The chown
   run hands the whole tree to your uid before syncing.
4. **Replicate:** `sync-model.sh` — `rsync -ah` (symlink-aware; the cache is
   symlinks, see Part 4) over `ssh -c aes128-gcm@openssh.com`, to the *same*
   path on the worker. Worker unreachable → skips with a warning, single-node
   until you sync. **Never under sudo** — root has no ssh key.
5. **Verify both nodes:** parses `model.safetensors.index.json`, takes
   `set(weight_map.values())`, checks each shard file exists. Dies
   `incomplete on <node>`. This is the real gate — not rsync's exit code.
   (Run it alone anytime: `rack verify <org/model>`. Head only — S3.)

**Gated repos:** license on the HF page once, then the token where the
*container* reads it: `printf '%s' 'hf_yourtoken' > ~/dgx/hf/token`. Partial
repos: add `--include 'SUBDIR/*' --local-dir …` to the download command inside
step 2 (H3's exact command is in `recipes/h3.env` — 144 GB of a 498 GB repo).
Weights pulled outside hub layout are referenced by absolute container path in
`MODEL=` instead of an org/id.

### `rack up <recipe>` — resolve, gate, dispatch

Recipe resolution: exact `recipes/<r>.env` → literal path → unique prefix
(`rack up inkling` finds `inkling-small-nvfp4`; ambiguity dies with the list).
Gates: template refused; `FILL_ME` refused. Routing: `TOPOLOGY` in `.env`
overrides everything (`solo` = single-box site, Part 11); on `auto`, one grep
decides the world:
`is_tp2` (anchored, comment-proof, inheritance-following) → `launch-cluster.sh`
or `launch-solo.sh`. Exactly one extra arg is honored: `--debug`, immediately
after the recipe name, cluster path only (launch-cluster reads `$2`; launch-solo
ignores everything past the recipe). Anything else you append is **silently
dropped** — it never reaches the serve command. Flag changes go in the recipe.

#### Solo path (launch-solo.sh), in order

1. cd repo root, source `.env`, source the recipe, die if no `MODEL`.
2. `ENV_EXTRA` → `-e` flags (after the baked ones, so recipe wins).
3. **Mods, solo flavor:** every file under `<mod>/overlay/` becomes a read-only
   bind-mount at its overlay-relative container path. No `run.sh` runs. A mod
   without `overlay/` dies before launch.
4. **Drop caches** (best-effort): `sync; echo 3 > /proc/sys/vm/drop_caches`.
   On unified memory, page cache and GPU allocations share one pool — skip
   this after a previous serve and a big load OOMs "with memory free".
5. `exec docker run --rm --name serve_solo --network host --gpus all --ipc=host
   --log-driver journald --ulimit nofile=1048576:1048576 …` — foreground.
   Ctrl-C stops *and removes* (that is `rack down`'s whole job too).
   journald keeps the crash trace after `--rm` deletes the container:
   `journalctl CONTAINER_NAME=serve_solo --since "2 hours ago"`.
6. Mounts: the HF cache, then `~/.cache/vllm`, `~/.cache/flashinfer`,
   `~/.triton` — the compile caches. **This is why second boots are fast.**
   Deleting these directories = every model pays full compile again.
7. Command: `vllm serve $MODEL --host 127.0.0.1 --port $API_PORT ${SERVE_ARGS[@]}`.
   Recipe flags come after, so a recipe's `--host`/`--port` **wins** (h3 → 8091;
   spark-2 variants → fabric bind `192.168.100.2`).

#### Cluster path (launch-cluster.sh), in order

Sanity gates, all *before* anything starts:
- **Right box:** IP on `$FABRIC_IF` must equal `$HEAD_IP`.
- **Worker ssh:** BatchMode, 5 s.
- **Image parity:** `docker image inspect --format '{{.Id}}'` both sides must
  match — divergent builds die mid-load with rendezvous/kernel confusion, so
  this makes it loud: `image differs between nodes — run scripts/build.sh first`.
- **Mods exist** (cluster mods are `run.sh`-driven; no overlay/ requirement).

Then: drop caches both nodes → **idle keep-alive container per node**
(`--entrypoint= … sleep infinity` as PID 1, `--privileged` for RDMA verbs +
memlock, same cache mounts, env from `env_flags` including all NCCL pinning) →
**capability probe** `EngineArgs.nnodes` inside the head container (probe
EngineArgs, NOT `--help` — 0.26.0 accepts `--nnodes` but hides it there; NGC's
0.24 fails here: `this image's vLLM lacks native multi-node…`) → mods applied
inside both containers (`docker cp` + `chmod +x run.sh && ./run.sh`, fail-hard)
→ two generated scripts, byte-identical `vllm serve $MODEL ${SERVE_ARGS}` plus
exactly four topology flags:

```
--nnodes 2 --node-rank {0|1} --master-addr $HEAD_IP --master-port $MASTER_PORT
                                        (+ --headless on rank 1)
```

**Worker first** (detached, output → `/proc/1/fd/1` so it lands in
`docker logs serve_node` on spark-2), **head second, foreground** — worker-first
avoids the rendezvous race. `trap cleanup INT TERM EXIT` stops both containers
on any exit path; `--rm` removes them. No Ray; never pass
`--distributed-executor-backend`.

**First cluster run of any new config: add `--debug`** (`rack up <r> --debug`)
→ `NCCL_DEBUG=INFO`. The gate is a log line, not a feeling:

```
NCCL INFO … NET/IB       RDMA. Real.
NCCL INFO … NET/Socket   TCP fallback. STOP — every number after this is invalid.
```

Also correct: `Model loading took ~<half> GiB` per node (TP splitting is
working) and the benign-but-alarming `No available shared memory broadcast
block found in 60 seconds` (one rank compiling while the other waits — wait).

### `rack status` / `rack logs` / `rack chat` / `rack bench` / `rack down`

- **status**: containers (`docker ps` filtered `serve_`), memory (`free -g` —
  the only true instrument on unified memory), API (`/v1/models` on the head).
  Distinguishes `nothing serving` from `UNKNOWN — could not look`; treat
  UNKNOWN as "go look yourself", never as idle.
- **logs**: head, following: `/tmp/*-launch.log` then `serve_node` then
  `serve_solo`; not following: `serve_node` then `serve_solo` then the launch
  log (the launch log drops to last; container order doesn't change). Worker:
  `rack logs worker` → `ssh spark-2 docker logs --tail 60 serve_node`.
  Head-local only (S3).
- **chat**: asks `/v1/models` for whatever is serving, streams one completion
  (max_tokens 1024, 600 s). No model argument by design.
- **bench**: same discovery, then `bench.py --url $API --model <discovered>
  --label <label or timestamp>`; extra args pass through (`rack bench mylabel
  --runs 5 --max-tokens 512`).
- **down**: `docker stop serve_node serve_solo` locally + `serve_node` on the
  worker. `--rm` makes stop == cleanup. Prints `stopped` unconditionally —
  it is not a verification; `rack status` is.

### `rack build [ngc|upstream|community]`

Runs ON spark-1 (native aarch64 build). Profiles:

| Profile | Base | When |
|---|---|---|
| `ngc` | `nvcr.io/nvidia/vllm:26.07-py3` (vLLM 0.24) | phases 1–2; CANNOT serve Inkling or multi-node `--nnodes` |
| `upstream` | `vllm/vllm-openai:v0.26.0-aarch64-cu129-ubuntu2404` | Inkling-capable; installs extras (scipy, instanttensor); NCCL redirect guard matters here |
| `community` | `eugr/spark-vllm:latest` (**rack's no-arg default**) | the field-proven sm_121 build; upstream's cu129 wheels lack sm_121 kernels on the FA4/tvm_ffi path — `no kernel image is available`, measured 2026-07-31. Moving tag: build.sh records the digest at build time |

The Dockerfile adds two things to any base: the **NCCL redirect guard** (pip-
wheel NCCL hangs multi-node on Spark, vllm#42354 — if a system libnccl exists,
the pip copy is symlinked to it; verify the build log printed `nccl: redirected`)
and, with `INSTALL_EXTRAS=1`, scipy (Inkling's vision tower imports
`linear_sum_assignment`; missing = ModuleNotFoundError at load) + fast loaders
(torch pinned first so pip can't swap in a CPU wheel).

Then image sync: `docker save | ssh -c aes128-gcm | docker load`, skipped when
image IDs already match. `--no-sync` to build only.

---

## Part 4 — Where models live (and how to manipulate them by hand)

```
~/dgx/hf/hub/models--Org--Name/
├── refs/main                 → commit sha you have
├── snapshots/<sha>/          real filenames — ALL SYMLINKS into blobs/
├── blobs/<sha256>            the actual bytes, content-addressed
├── trees/<sha>.json          hub metadata (the mode-600 rsync killers)
└── .no_exist/<sha>/          negative cache — files known absent
```

Consequences: `du` a snapshot and you measure pointers (use `du -shL` or du the
blobs); copy with symlink-aware tools only (`rsync -ah` — never `scp -r` or
`cp` without `-a`); two revisions sharing a tokenizer share one blob. Weights
outside hub layout (H3's `local/MiniMax-H3/FL2VA`) are referenced by container-
absolute `MODEL=` path and replicated with plain rsync of that directory.

**By hand, when rack isn't enough:**
- delete a model: `rm -rf ~/dgx/hf/hub/models--Org--Name` (both nodes)
- inspect what a snapshot really costs: `du -sh ~/dgx/hf/hub/models--*/blobs`
- count expected shards: `python3 -c "import json,glob;i=json.load(open(glob.glob('$HOME/dgx/hf/hub/models--Org--Name/snapshots/*/model.safetensors.index.json')[0]));print(len(set(i['weight_map'].values())))"`
- re-sync one model: `scripts/sync-model.sh Org/Name` (then `rack verify Org/Name`)

---

## Part 5 — Recipe anatomy: every knob, how to set it

A recipe is bash, sourced by the launcher: `MODEL=` (string),
`SERVE_ARGS=(…)` (array — arrays survive quoting, which is why JSON-valued
flags work), `ENV_EXTRA=(…)` (KEY=VAL), `MODS=(…)` (dirs), optional `IMAGE=`
override, optional `. recipes/parent.env` inheritance (append with
`SERVE_ARGS+=(…)`; last flag wins).

### Sizing knobs — decide fit and startup. Raising them never adds speed.

**`--gpu-memory-utilization`** — fraction of the unified pool vLLM claims.
House values: `0.7` first boot of anything new on a fresh image (NGC known
issue: shared-memory OOM), `0.8` production ("near the edge"), `0.845` proven
on DSpark at 1M ctx. **Never raise past 0.8 to fix a wedge** — higher steals
from the OS/page cache and *causes* the OOM-with-memory-free trap. A wedged
(not cleanly OOMed) node is unified-memory pressure: lower the next two knobs
instead, and don't debug it live.

**`--max-model-len` × `--max-num-seqs`** — one KV pool, two claimants.
**Never raise both.** Decide what the workload needs and starve the other:
long-context single user → big len, seqs 2–4 (dsv4: 1048576 × 4, possible only
because fp8 MLA KV is compact — measured pool 1,294,708 tokens); agent swarm →
modest len, sweep seqs for the aggregate knee (DSpark: 66 tok/s @1 → ~150 @6).
Cost of big len is not memory but prefill wall-time: minutes of TTFT on
hundred-k-token prompts.

**`--kv-cache-dtype fp8`** — buys context, not speed. Required for DeepSeek:
the `fp8_ds_mla` layout **rejects** `'auto'` — read the error text, it names
the layout.

**`--quantization`** — when *load-time* quantization made the fit possible, it
is MANDATORY and the recipe must say so (h3: fp8 halves the DiT at load). For
pre-quantized checkpoints it depends on the format: MXFP4 (gpt-oss) and fp8
(dsv4, dsv4-ab) need no flag, but NVFP4 compressed-tensors checkpoints still
pass `--quantization nvfp4` explicitly (inkling — GB10-native, vllm#49258).
When scaffolding a new pre-quantized model, check what the working recipe for
that format does, not just this rule.

**`--tensor-parallel-size 2`** — only when it doesn't fit on one node. This
flag alone is what routes `rack up` to the cluster launcher. Active line only —
a commented copy is ignored (and must stay ignored).

### Dialect knobs — output correctness. Nothing crashes when you skip these; you just serve a silently lesser model.

`--trust-remote-code` (model ships custom code), `--tokenizer-mode`,
`--enable-auto-tool-choice`, `--tool-call-parser`, `--reasoning-parser`.
The rule: **read the tokenizer implementation in the engine, not the model
card.** The measured case: DeepSeek's docs say thinking defaults on; vLLM's
`deepseek_v4.py` says `thinking = kwargs.get("thinking", False)`. Both true.
Only one runs here. So the parser waits forever for a `<think>` span the model
was never told to emit — request it per-call:
`"chat_template_kwargs": {"thinking": true, "reasoning_effort": "max"}`
(effort strings other than `max` all coerce to `high`; effectively on/off).
And once tool calls are in play, `reasoning_content` **must be echoed back** on
the next request or the model 400s — most clients strip it to save context and
break on hop two. Per-request settings are not flags, but they live in the
recipe as comments so the knowledge survives.

Diffusion/omni engines have no dialect layer — h3 deletes the whole section.
Its correctness knob is the attention backend instead (`--diffusion-attention-
backend`: unset = CUDNN_ATTN on sm_12x; docs say FLASH_ATTN but that wraps FA4
= datacenter Blackwell, not GB10; SAGE_ATTN = 2.12× faster, same peak memory,
**same-seed output diverges** — INT8 numerics compound over the denoise).

### Compile & timeout knobs

**`--compilation-config '{"cudagraph_mode":"PIECEWISE","custom_ops":["all"]}'`**
— the standing answer on cross-node GB10. FULL_AND_PIECEWISE compiled 4.75 h
without converging (measured 2026-07-31). Ladder: PIECEWISE → `--enforce-eager`
(diagnostic only — isolates graphs vs everything else, slow).

**`--init-timeout` / `--stage-init-timeout`** — H3 measured 605 s init against
the 600 s default and was killed *ready*. Recipes carry 1800. If a load is slow
and the engine dies at the moment it should have come up, suspect the timeout
before the model.

### ENV_EXTRA vars seen in production

| Var | Recipe | Why |
|---|---|---|
| `LAMPORT_RS_SCONV=0` | inkling | **Mandatory on RoCE** — the Lamport fused reduce-scatter assumes NVLink; hard error without it |
| `VLLM_USE_AOT_COMPILE=1` | inkling | compile artifacts cached on disk → later boots much faster |
| `VLLM_USE_BREAKABLE_CUDAGRAPH=0` | inkling | experimental capture path off on GB10 |
| `VLLM_WORKER_MULTIPROC_METHOD=spawn` | h3 | omni worker spawn method |
| `VLLM_OMNI_VIDEO_SYNC_TIMEOUT=7200` | h3 | sync endpoint ceiling (console polls async anyway) |

### MODS — the patch layer

A mod is a directory. **Solo:** its `overlay/` tree is bind-mounted file-by-file
read-only over the image (no rebuild, no run.sh). **Cluster:** the dir is copied
into both containers and its `run.sh` executed, fail-hard. Every mod records
three things or it doesn't merge: what it fixes, the upstream ref, **the
deletion condition**. Current: `mods/inkling-sm12-paged-kv` (FA4 sm_12x paged-KV;
without it every rank dies in warmup `Paged KV not supported on SM 12.0`;
delete when flash-attention#2348 lands — fetch pinned+licensed via
`scripts/fetch-inkling-mod.sh`) and `mods/h3-fp8-0.26` (H3-aware fp8 merged 27 h
after the v0.26.0 tag; delete at the first post-08-04 omni release).
Corollary, learned as a zombie engine: **never serve on an engine whose patch
failed to import** — gate builds on an import check (Dockerfile.h3-sage does:
a dud can't tag).

### Writing a recipe, start to finish

The knobs above are the vocabulary; this is the sentence. Worked as you would
actually type it, for a hypothetical new chat model `org/NewModel-30B-FP8`.
The completed exams to crib from: `dsv4.env` (chat, every dialect trap),
`inkling-small-nvfp4.env` (TP=2 + mod + MTP), `h3.env` (different engine
entirely), `h3-spark2-sage.env` (the two-line variant done right).

**Step 1 — fit, before anything exists:**
```bash
rack fit org/NewModel-30B-FP8
```
Read all four facts: GB on disk, params (check effective bpw — ~8 means fp8
pre-quantized, ~16 means bf16 and twice the resident size), active params if
MoE, context. Verdict says which node count. **Write the numbers down now** —
they go in the recipe header, and if the verdict was "fits because you'll
quantize at load," redo the arithmetic against the quantized size by hand.

**Step 2 — engine, before scaffolding:** the question is whether a build
supporting this model exists *for this arch as of today*. Check the model's
vLLM support PR date against your image's vLLM version (`docker run --rm
--entrypoint python3 $IMAGE -c "import vllm; print(vllm.__version__)"`).
Answer decides `IMAGE=` (leave unset = repo default) and possibly the build
profile. If the model is newer than every image you have: stop here — a recipe
cannot fix a missing engine.

**Step 3 — scaffold:**
```bash
rack new newmodel org/NewModel-30B-FP8
```
Opens with every question slotted, `MODEL`/`--served-model-name`/`--port`
filled, sizing flags as `FILL_ME` (launch refuses until you answer).

**Step 4 — edit top to bottom.** For each slot:
- *1 FIT*: transcribe step 1's numbers and verdict.
- *2 ENGINE*: `supported since vLLM x.y / PR #nnn; image chosen: <which>
  because <why>`.
- *3 ACCESS*: gated? token placed? partial pull needed? Record the exact
  download command if non-default.
- *4 SHAPE*: TP only if the fit says two nodes. `--gpu-memory-utilization 0.7`
  for the very first boot, 0.8 once it's proven. The FILL_ME pair: decide
  workload first — long-context (big len, seqs 2–4) or swarm (modest len,
  sweep seqs later). `--kv-cache-dtype`/`--quantization` per Part 5's rules.
- *5 DIALECT*: open the model's tokenizer/parser files in the engine
  (`docker run --rm --entrypoint bash $IMAGE -c "ls
  /usr/local/lib/python3*/dist-packages/vllm/tokenizers/ | grep -i <family>"`,
  then read the file). Does thinking default on? Is there a named tool-call
  parser? Any `chat_template_kwargs` the client must send? Write what you
  find, including per-request settings as comments. No parser exists → leave
  the flags out and note it — an honest gap.
- *6 ENV*: start from the template's PIECEWISE default on this rack; ENV_EXTRA
  only for measured needs. These are **site answers** — flag them as such
  (see Part 11).
- *7 PATCH*: hopefully `MODS=()`. If not: what, upstream ref, deletion
  condition — all three or it doesn't merge.

**Step 5 — pull, launch, prove:**
```bash
rack pull org/NewModel-30B-FP8
rack up newmodel                 # first cluster run of a new config: --debug
rack logs -f                     # until: Application startup complete.
rack chat "reply with exactly: RECIPE OK"
rack bench newmodel-baseline
```
Transcribe bench's medians into slot 8 with the date. **A recipe without a
number in slot 8 is not finished** — it's a command that started once.

**Step 6 — the failure ladder.** Everything that went wrong on the way here
gets recorded *in the recipe, at the flag it concerns* — symptom, cause, fix.
That's the difference between this file and a gist.

---

## Part 6 — Measurement (question 8, mechanized)

**`rack bench [label] [bench.py args]`** → `scripts/bench.py`. Flags: `--url`
(default `http://127.0.0.1:8000/v1` when run bare — rack passes the right one),
`--model` (required), `--label` (required), `--runs 3`, `--max-tokens 256`,
`--timeout 600`, `--prompt` (MoE-explainer default), `--no-warmup`. One
unrecorded warmup, then N runs, each appended **immediately** to
`bench/results.jsonl`:

```json
{"ts","label","model","url","max_tokens","run","git","ttft_s","decode_tok_s","total_s","completion_tokens","counted_by"}
```

`ttft_s` = request-sent → first content chunk (includes queueing + prefill).
`decode_tok_s` = `(toks−1)/(tlast−tfirst)`. Token count prefers the server's
`usage`; falls back to chunk count — which under speculative decoding
**undercounts** (one chunk can carry several tokens), so always bench MTP
configs with `usage` present. The prompt is NOT recorded: change the prompt and
your labels are silently incomparable — encode prompt changes in the label.

**The tuning loop (docs/04):** baseline → change ONE lever → bench same way →
commit config + both numbers ("no number, no merge") → didn't move? revert,
keep the negative result. The ceiling to reason against: GB10 decode is
memory-bandwidth-bound, ~273 GB/s LPDDR5x/node; per token a TP=2 MoE reads
`(active_params × bpw ÷ 8) ÷ 2` per node + KV. Every real lever (a) reads fewer
bytes/token, (b) reads them less often, or (c) hides non-read time. MTP is (b)
— the biggest unlock; thinking-mode on/off moved DSpark decode 1.32× and MTP
acceptance 24→40% — pin thinking mode in benchmarks and always log acceptance.

Heavier harness (already in the image): `docker exec serve_node vllm bench serve
--backend openai-chat … --ignore-eos --percentile-metrics ttft,tpot,itl,e2el`
— `--ignore-eos` + random prompts defeat prefix-cache flattery; decode tok/s ≈
1000 / TPOT-p50-ms; never compare across image tags silently.

---

## Part 7 — The video path (h3), where everything mutates

Different engine (vLLM-Omni, `--omni`, `/v1/videos` multipart), recipe overrides
`IMAGE`, solo only (no multi-node story), port 8091 loopback (spark-1) or
fabric-bind (spark-2 variant). `rack chat`/LiteLLM don't apply; `rack status`
polls 8888 and will say "not answering" while h3 is healthy on 8091 — check
`curl -s http://127.0.0.1:8091/health`.

Request-time knobs (not flags): duration (**the expensive one** — attention is
quadratic in frames; ≤8 s per clip on one Spark, 15 s OOMed AND killed the
result-pump thread → zombie: health 200, queue dead, restart = Ctrl-C +
`rack up h3`), resolution, `num_inference_steps` (50 reference, 10 validated
floor; nearly linear in render time — duration is the quadratic knob).
Measured: 4 s/50 steps = 508 s; 5 s/30 = 596 s warm; SAGE 281 s; first render
per engine +25–40 min compile.

`scripts/sequence.py <shots.json>` chains shots (fl2va from the previous shot's
last frame — the whole continuity trick), scenes parallelize across an engine
pool, resumes by mp4-existence (run it from the repo root — the workdir is
CWD-relative; wrong cwd = fresh workdir = re-renders everything), retries reads
but never submissions (a timed-out submit may have created a job — blind retry
= double render), aborts at 2.5× the engine's best shot time or 3600 s ("restart
BOTH engines, re-run the same command — finished shots are kept"), and **always
re-encodes the stitch** (stream-copy concat exits 0 and freezes at every
boundary — measured). Engine job stores are container-scratch: undownloaded
clips die on restart; `sequences/<film>/` is yours. `rack scrub [--delete]`
inventories/removes every trace a run leaves (render dirs, untracked shot
lists, prompt-bearing `/tmp` logs, loose media, engine jobs — **spark-1 only**:
scrub queries `127.0.0.1:8091`, which the fabric-bound spark-2 engine does not
answer, so its job store is silently skipped; restart that engine to clear it,
or DELETE against `http://192.168.100.2:8091`. journald stays manual —
`--delete` recognized in position 1 only).

---

## Part 8 — Failure catalogue: symptom → cause → fix

Fabric & cluster:

| Symptom | Cause | Fix |
|---|---|---|
| `ibv_modify_qp failed with 61 No data available` on restart | a **stored** GID index somewhere; table renumbered on interface bounce | delete it; NCCL ≥ 2.21 auto-selects. Hardware is fine — do not debug cables |
| `NET/Socket` in NCCL log | RDMA unreachable — `NCCL_IB_HCA` wrong/unset | every number after is invalid. `IB_HCAS` must list both twins; cluster containers are `--privileged` via the script |
| Hangs at NCCL init; ping works | ufw is per-interface; a rail is firewalled | `allow in on` **both** fabric interfaces (preflight checks) |
| nccl-tests plateau ~100 Gb/s | one RoCE twin in `NCCL_IB_HCA` | both: `rocep1s0f0,roceP2p1s0f0` |
| `vLLM lacks --nnodes` abort at launch | NGC image, vLLM 0.24 | `rack build upstream` (or community) |
| Multi-node hang on pip-wheel image | bundled pip NCCL (vllm#42354) | Dockerfile guard; verify build log printed `nccl: redirected` |
| `image differs between nodes` | stale image on one side | `rack build` (sync is automatic) |
| Worker "silent" | it logs on its own box | `rack logs worker` |
| `No available shared memory broadcast block found in 60 seconds` | one rank compiling, one waiting | benign — wait |
| Rendezvous hang, no error | `MASTER_PORT` blocked/occupied, or wrong `HEAD_IP` | check the port between nodes; check `.env` |

Memory (the boss fight):

| Symptom | Cause | Fix |
|---|---|---|
| OOM with `free` showing room | page cache shares the unified pool | `sync && sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'` both nodes; want ~115 GiB free for the big models |
| Load stalls near the end | cache crowding during load | drop caches again mid-load (60 s loop is legitimate) |
| Worker killed mid-load | earlyoom on transient pressure | disable earlyoom for cluster runs |
| Node wedges, not clean OOM | unified-memory pressure | lower `--max-model-len` or `--max-num-seqs`; **never** raise `--gpu-memory-utilization` past 0.8; don't debug live |
| `nvidia-smi` shows nothing useful | unified memory — it's blind here | `free -h` is the only truth |

Model & engine:

| Symptom | Cause | Fix |
|---|---|---|
| `Paged KV not supported on SM 12.0`, all ranks die in warmup | Inkling FA4 needs the sm120 paged-KV patch | `scripts/fetch-inkling-mod.sh`; mod in `MODS` |
| Startup dies capturing CUDA graphs | FULL variants exhaust memory on cross-node TP | PIECEWISE; then `--enforce-eager` as diagnostic |
| `LAMPORT`/reduce-scatter errors | fused path assumes NVLink | `LAMPORT_RS_SCONV=0` in ENV_EXTRA — mandatory on RoCE |
| `no kernel image is available` | upstream cu129 wheels lack sm_121 on that path | community profile image |
| `ModuleNotFoundError` (scipy) at Inkling load | official image ships without it | build with extras (upstream profile does) |
| No `reasoning_content` though parser is set | engine default: thinking off | per-request `chat_template_kwargs: {"thinking": true}` |
| 400 on the second hop of a tool conversation | client stripped `reasoning_content` | echo it back — DeepSeek requires it |
| Engine dies exactly at readiness after a long load | init beat by a timeout | raise `--init-timeout`/`--stage-init-timeout` (H3: 605 s vs 600 default) |
| Health 200 but every request dies / queue dead | zombie after an OOM'd render or armed-but-broken backend | restart the engine; gate builds on import checks |
| First request after boot is garbage/slow (~30 s+) | JIT warmup | throwaway request first, always; bench warmup does this |
| Load fails with an offline / cache-miss error at launch (it *cannot* download — `HF_HUB_OFFLINE=1` is baked in) | weights not in `$HF_CACHE` (or wrong path) | `rack pull` first; engines are offline by design |
| rsync exit 23 mid-replication | root-owned 600 hub metadata | `rack pull` chowns now; by hand: chown the tree, re-sync, **re-verify** |

Tooling self-awareness (the tool lying to you):

| Symptom | Truth |
|---|---|
| `status`/`models` print `UNKNOWN — could not …` | the probe failed; state is unknown, NOT idle. Go look on the node |
| `stopped` printed | unconditional — verify with `rack status` |
| `rack status` says not answering while h3 serves | it polls 8888; h3 is on 8091 |
| bench `counted_by: "usage"` but odd token counts | usage object present but lacked `completion_tokens` → silently counted chunks |
| worker halves of preflight/scrub/net print a `cd: … No such file or directory` line and no check output | worker checkout not at `~/dgx/dgx-spark-serve` — `|| true` swallows the exit code, so rack carries on as if it succeeded |

---

## Part 9 — Recovery drills

**Cold start (power loss, both nodes):**
`rack preflight` (all-PASS both nodes, or fix what it names — every check maps
to a failure above) → `rack status` (expect nothing serving) → `rack up <recipe>`
→ solo: watch `rack logs -f` for `Application startup complete.`; cluster: watch
the `rack up` terminal itself — the head's log exists only there (S2), and
`rack logs worker` shows the worker half → throwaway request → serve. Usually
nothing needs manual cleanup: containers were `--rm`, and the **cluster**
launcher `docker rm -f`s leftovers itself. The solo launcher does not — if
`docker run` dies with a name conflict, `docker rm -f serve_solo` by hand first.

**Engine crashed / wedged / zombie:** `rack down` (or Ctrl-C the foreground
terminal) → if memory looks occupied with nothing running: drop caches → `rack
up`. Crash trace: solo → journald; cluster worker → `rack logs worker`; cluster
head → your terminal scrollback (S2 below).

**A launch dies mid-way (cluster):** the EXIT trap already stopped both
containers. Read the error, match Part 8, fix, relaunch — idempotent by design.

**Fabric suspect:** `rack preflight` first (carrier, MTU, IPv4 per rail,
phys_switch_id, GID resolves, ufw both rails, peer ping+ssh). Then a `--debug`
launch and read the NET/IB line. Bandwidth proof: `ib_write_bw` per rail (~109)
and both (~196).

**"Is anything leaking to the internet?":** `rack net` — in-container /proc
audit (host-side `ss` without sudo silently lies "no connections"). Verdict
line per node.

**Worker checkout stale/diverged:** on spark-2: `git stash && git pull` (origin
= spark-1). Image stale: `rack build` re-syncs automatically on ID mismatch.

**Replicate a working deployment elsewhere:** copy the recipe + this repo;
the recipe carries every model-specific fact with its reason; `.env.example`
carries every hardware fact. That pairing is the whole point of the layering.

---

## Part 10 — Sharp edges (current, known, real)

S1. **Cluster ignores `API_PORT`.** launch-cluster defines it and never uses
it; the generated serve command carries no `--host`/`--port`. Every TP=2 recipe
must set them explicitly. A TP=2 recipe without them binds vLLM's defaults
(`0.0.0.0:8000`) on a `--privileged` host-network container — wrong port AND
wide open. This was live in the repo until 2026-08-09: `phase2-gpt-oss-120b.env`
shipped without either flag (fixed; dsv4, dsv4-ab, inkling always had them).
When writing a new TP=2 recipe, the two flags are not optional.

S2. **Cluster head logs are ephemeral.** The head's serve runs as a foreground
`docker exec` with no redirect, and cluster containers don't use journald (solo
does). After a head crash + teardown, the only trace is your terminal
scrollback. Keep the launch terminal in tmux.

S3. **`verify`, `logs`, `bench`, `chat` are head-local.** They use local docker
/ `127.0.0.1` (unlike `status`/`models`, which ssh). From the Mac, `verify`
inspects the Mac's filesystem and dies `incomplete on spark-1` even when
spark-1 is complete. Run them on spark-1.

S4. **`stop-cluster` container names are literals.** Rename `serve_node`/
`serve_solo` anywhere and stop silently stops nothing while printing `stopped`.

S5. **`is_head` matches `HEAD_IP` as an unanchored pattern** — an interface
carrying `192.168.100.10` also matches `192.168.100.1`. Exotic, but if rack
ever behaves like it thinks your laptop is the head, this is why.

S6. **`rack new`'s sed fills are full-line-anchored.** If TEMPLATE.env drifts
(the port line stops being exactly `  --port 8888`), scaffolds silently stop
filling that slot. After editing the template, scaffold a throwaway and check.

S7. **Inheritance depth caps at 4** — deeper chains silently classify solo.
Keep recipe chains ≤ 3 links (current max is 3).

---

## Part 11 — Lift and shift: the same stack on a rented GPU box

The layering rule was the portability plan all along: recipes hold **model
truth**, `.env` + launchers hold **site truth**. Moving to a rented GB300 or
RTX Pro box is not a port — it's a new `.env` plus re-answering the recipe
questions that were always site-local.

### Which of the eight questions re-open on new hardware

| Q | Ports unchanged? | What re-opens |
|---|---|---|
| 1 FIT | **re-run** | new `USABLE_GB`; the arithmetic is the same |
| 2 ENGINE | **re-answer** | "supports this model for sm_121/aarch64" becomes "…for sm_120/x86" or "…for sm_103/aarch64" — *different dates*. The whole question restated, not skipped |
| 3 ACCESS | ✓ | tokens/partitions/download commands port verbatim |
| 4 SHAPE | mostly | flags port; **`--gpu-memory-utilization` semantics change** — 0.7–0.8 was a unified-memory scar; on discrete HBM/GDDR, vLLM's 0.90 default is normal and 0.95 common |
| 5 DIALECT | ✓ | tokenizer/parser truths are engine facts, not hardware facts — the most valuable slice of every recipe ports untouched |
| 6 ENV | **fully re-answered** | PIECEWISE (a cross-node GB10 measurement), `LAMPORT_RS_SCONV=0` (a RoCE scar), the NCCL family — none are portable truths. Start a cloud port by *deleting* every Q6 line, then re-add what the new site proves it needs |
| 7 PATCH | **re-check** | sm-specific mods may be unnecessary (GB300) or still needed (RTX Pro — see below) |
| 8 PROOF | **re-measure** | day one on any new box: bench, label with the site, commit. `results.jsonl` becomes your cross-hardware ledger |

### The two hardware classes you named

**GB300 (Grace-Blackwell, datacenter):** aarch64 like the Sparks, ~288 GB
HBM3e per GPU, sm_103. This is the hardware FA4 actually targets — the entire
sm_12x kernel-coverage problem class (the inkling mod, the FLASH_ATTN-vs-CUDNN
h3 note, `no kernel image is available`) **disappears**. NVLink-class
interconnect means the Lamport collectives work as designed. Expect official
images to just work; your Spark scars are what to delete.

**RTX Pro 6000 Blackwell (workstation/server):** typically x86_64 hosts, 96 GB
GDDR7, **sm_120** — one minor revision from GB10's sm_121, which means it
*shares* the Spark's kernel-coverage scars: FA4 paged-KV gaps, sm_12x wheel
coverage. Question 7's answers may port where you'd rather they didn't. Check
the mod's own arch strings (the vendored bundle is literally named
`FA4-SM120`) before assuming either way. Cheaper per hour; more question-2
homework.

### Standing up a rented box, start to serve

```bash
# 1. the box: docker + nvidia-container-toolkit (most GPU rentals pre-install)
git clone https://github.com/bytebunkerlabs/dgx-spark-serve ~/dgx/dgx-spark-serve
cd ~/dgx/dgx-spark-serve
mkdir -p ~/dgx/hf ~/.cache/vllm ~/.cache/flashinfer ~/.triton

# 2. site truth — this is the entire port
cp .env.cloud.example .env
#    edit: USABLE_GB from nvidia-smi; IMAGE pinned per question 2

# 3. gated models: token where the container reads it
printf '%s' 'hf_yourtoken' > ~/dgx/hf/token

# 4. same lifecycle, same recipes
rack fit org/Model             # re-run question 1 against the new USABLE_GB
rack pull org/Model            # replication auto-skips: WORKER_SSH is empty
rack up <recipe>               # TOPOLOGY=solo routes TP through launch-solo
rack bench <site>-<model>-baseline   # question 8, day one, labeled by site
```

### Command deltas on a single-box site

| Command | Delta |
|---|---|
| `rack build` | skip — off-Spark, the pinned official multi-arch image (`IMAGE=` in `.env`) replaces the whole profile system |
| `rack pull` | works; replication auto-skips (empty `WORKER_SSH`), verify runs on the one node |
| `rack up` | `TOPOLOGY=solo` sends everything — TP included — through launch-solo. **TP=N in one box uses `--tensor-parallel-size N` exactly as before**; vLLM splits across local GPUs, no fabric, no `--nnodes` |
| `rack preflight` | the box checks (docker, nvidia runtime, disk, memory) still mean what they say; the fabric section will FAIL loudly and irrelevantly — on a box with no ConnectX, read it selectively. On x86 the aarch64 check FAILs too: expected |
| `rack status` | memory section reports **host RAM** — on discrete-GPU hardware `nvidia-smi` is the truth for the number that gates fit (the exact inverse of the Spark rule) |
| `rack bench/chat/logs` | unchanged — you're on the box, they were head-local anyway |
| `sync-model`, `stop-cluster` worker half, `rack net` worker half | inert without a worker |
| drop_caches | harmless no-op posture — page cache and GPU memory are separate pools here |

### What to strip from a ported recipe

Delete on the cloud copy (they are answers to a question the new site asks
differently): `--compilation-config PIECEWISE` (single-box graphs converge —
vLLM's default FULL_AND_PIECEWISE is the fast path the Sparks couldn't have),
`LAMPORT_RS_SCONV=0`, sm_12x `MODS` (GB300), and raise
`--gpu-memory-utilization` toward 0.90 *one boot at a time, benched*. Keep:
everything in questions 3, 5, and the host/port convention. The honest way to
manage both copies: one recipe file per site is fine — `newmodel.env` and
`newmodel-cloud.env` sourcing it and overriding, exactly like
`h3-spark2-sage.env` does — because a variant that only overrides is a diff
you can read.

The offline/telemetry quartet, the journald logging, the whole
measure-or-it-didn't-happen loop: those port because they were never about
Sparks. That's the test of the layering, and the reason this is a lift and
shift rather than a rewrite.
