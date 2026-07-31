# dgx-spark-serve

The serving layer for a two-Spark cluster, built from parts you can read.

This repo replaces the community toolkits (MiaAI-Lab's dspark-toolkit, eugr/spark-vllm-docker)
with our own minimal equivalent: one Dockerfile, a handful of scripts, and runbooks that explain
every flag. It is deliberately two-nodes-only, no generality — the point is to understand each
layer well enough to change it, then make it fast.

Host preparation (Docker, firewall, monitoring, overlay network) lives in
[bytebunkerlabs/dgx-spark-setup](https://github.com/bytebunkerlabs/dgx-spark-setup). This repo
starts where that ends: the fabric is validated, the boxes are clean, and we own everything from
the container up.

## Quickstart

Two DGX Sparks, cabled and validated ([host prep here](https://github.com/bytebunkerlabs/dgx-spark-setup)).
Everything below runs **on spark-1** unless labelled otherwise.

```bash
# 0. get the repo on both nodes, at the same path
git clone https://github.com/bytebunkerlabs/dgx-spark-serve.git ~/dgx/dgx-spark-serve
cd ~/dgx/dgx-spark-serve && cp .env.example .env      # edit IPs/interfaces if yours differ
rsync -a --exclude mods . spark-2:dgx/dgx-spark-serve/

# 1. gate: both nodes must be all-PASS before anything else
scripts/preflight.sh
ssh spark-2 'cd dgx/dgx-spark-serve && scripts/preflight.sh'
```

### Serve something small first (one node)

```bash
scripts/build.sh --profile ngc --no-sync
scripts/launch-solo.sh recipes/phase1-qwen3-8b.env
# in another shell:
scripts/bench.py --model Qwen/Qwen3-8B --label baseline
```

### Serve Inkling-Small across both nodes

```bash
# image: the community profile is the one with sm_121 kernels (see below)
scripts/build.sh --profile community          # builds here, ships to spark-2

# the sm_12x attention patch, pinned with provenance
scripts/fetch-inkling-mod.sh

# weights: 171 GB, needed on BOTH nodes at the same absolute path
hf download thinkingmachines/Inkling-Small-NVFP4
scripts/sync-model.sh thinkingmachines/Inkling-Small-NVFP4

# free the machines — this model wants essentially all of both
scripts/stop-cluster.sh
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
ssh spark-2 "sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"

# launch (add --debug on the first run to see NCCL choose its transport)
scripts/launch-cluster.sh recipes/inkling-small-nvfp4.env --debug
```

First boot is slow: ~85 GB streamed per node plus kernel compilation. You're
waiting for `Application startup complete.`

**The gate that matters** is a log line, not a feeling:

```
NCCL INFO ... NET/IB       ← RDMA. Real.
NCCL INFO ... NET/Socket   ← silent TCP fallback. Stop; every number after this is invalid.
```

Then measure it:

```bash
scripts/bench.py --url http://127.0.0.1:8888/v1 --model inkling-small --label inkling-baseline
curl http://127.0.0.1:8888/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"inkling-small","messages":[{"role":"user","content":"hello"}],"max_tokens":64}'
```

Stop everything with `scripts/stop-cluster.sh`.

### If it fails

| symptom | cause | fix |
|---|---|---|
| `no kernel image is available` | image lacks `sm_121` kernels | `--profile community` |
| `Paged KV not supported on SM 12.0` | FA4 patch missing | `scripts/fetch-inkling-mod.sh` |
| `No module named 'scipy'` | official image omits it | `--profile community`, or add scipy |
| startup hangs for hours | FULL cudagraph capture | recipe already uses `PIECEWISE`; last resort `--enforce-eager` |
| `NET/Socket` in the logs | RDMA unreachable | check `IB_HCAS` lists **both** RoCE twins; ufw allows both fabric interfaces |
| worker looks silent | it logs on its own box | `ssh spark-2 docker logs -f serve_node` |
| OOM with memory apparently free | page cache | `drop_caches` on both, then retry |

Full walkthrough with the reasoning behind each flag: [`docs/`](docs/) —
`00-networking` (the fabric), `01-solo`, `02-cluster`, `03-inkling`, `04-tuning`.

## The stack, bottom to top

| layer | what | where it's decided |
|---|---|---|
| hardware | 2 × GB10, 128 GB unified memory each, 256 GB pooled | fixed |
| fabric | ConnectX-7 direct QSFP, two PCIe rails, RoCEv2 — 196 Gb/s measured | `docs/00-networking.md` |
| container | our image: NGC vLLM base + our launch layer | `Dockerfile` |
| engine | vLLM — paged KV, CUDA graphs, quantized MoE | `recipes/*.env` |
| distribution | NCCL over RoCE, tensor-parallel 2 | `scripts/launch-cluster.sh` |
| serving | OpenAI-compatible API, behind the existing LiteLLM gateway | gateway config (unchanged) |
| measurement | `scripts/bench.py` → `bench/results.jsonl` | `docs/04-tuning.md` |

## Layout

```
Dockerfile              the one image both nodes run
.env.example            every knob, commented; copy to .env
scripts/
  preflight.sh          read-only sanity check — run on both nodes before anything
  gid-index.sh          resolve the RoCE v2 GID index from hardware (never store it)
  sync-model.sh         replicate a model head → worker over the fabric
  bench.py              single-stream benchmark; appends to bench/results.jsonl
  build.sh              build the image and copy it to the worker
  launch-solo.sh        one node, one model
  launch-cluster.sh     TP=2 across both nodes (worker first, then head)
  stop-cluster.sh
docs/
  00-networking.md      the fabric: what preflight checks and why
  01-solo.md            phase 1 runbook
  02-cluster.md         phase 2 runbook
  03-inkling.md         phase 3 runbook — the capstone
  04-tuning.md          phase 4 — the performance program
recipes/                one env file per model = one reviewed serving profile
bench/results.jsonl     the lab notebook — committed, append-only
```

## Phases

Each phase has a **gate**: a command whose output proves the phase works. Don't move on until
the gate is green.

| phase | goal | gate |
|---|---|---|
| 0 | fabric + hosts ready (done once by dgx-spark-setup) | `scripts/preflight.sh` all-PASS on both nodes |
| 1 | solo: our image serves a small model on spark-1 | a completion returns; baseline in `bench/results.jsonl` |
| 2 | cluster: TP=2 across both, with a model that also fits on one | `NET/IB` in NCCL logs (never `NET/Socket`); bench vs phase 1 |
| 3 | Inkling-Small-NVFP4 — 266 B total / ~12 B active, 171 GB, needs both boxes | **passed 2026-07-31: 29.79 tok/s, TTFT 229 ms** |
| 4 | the loop: one lever at a time, measured | every change lands with before/after numbers in the commit |

Phase 2 deliberately uses a model that fits on one node — that makes the cost of distribution
*measurable* (solo vs TP=2 on identical weights) instead of an article of faith.

## Conventions

- `spark-1` = head, `192.168.100.1` on the fabric — the box you type on.
  `spark-2` = worker, `192.168.100.2`. Second rail: same boxes, `192.168.101.x`.
- The HF cache lives at `~/dgx/hf` at an **identical absolute path on both nodes**.
- Every runbook command is labelled **on spark-1**, **on spark-2**, or **on both**.
- Scripts are idempotent and safe to re-run unless a runbook says otherwise.
- Memory truth on unified hardware is `free -h`, never `nvidia-smi`.

## Measurement discipline

`bench/results.jsonl` is append-only and committed. Every tuning change follows the same loop:
bench → change one thing → bench → commit with the numbers in the message. If a change doesn't
move a number, it reverts. The interconnect lesson that started this project — doubling fabric
bandwidth moved decode by 0.15% — came from exactly this discipline.

## Image profiles (the one decision with a fork in it)

| profile | base | good for | why not everything |
|---|---|---|---|
| `ngc` | `nvcr.io/nvidia/vllm:26.07-py3` | phases 1–2 | ships vLLM **0.24** — predates Inkling support and the native `--nnodes` multi-node flags |
| `upstream` | `vllm/vllm-openai:v0.26.0` (aarch64) | Inkling-aware, but **not on GB10** | cu129 wheels carry no `sm_121` kernel images on the FA4 path — dies with `no kernel image is available` |
| `community` | `eugr/spark-vllm` | **phase 3 — the one that works** | vLLM `main` compiled for `sm_121` (`TORCH_CUDA_ARCH_LIST=12.1a`), NCCL and FlashInfer rebuilt to match. Moving tag; `build.sh` records the digest |

`scripts/launch-cluster.sh` probes the image for `--nnodes` before launching and
refuses with an explanation rather than failing mid-rendezvous.

## Measured

| model | placement | decode | TTFT |
|---|---|---|---|
| Inkling-Small-NVFP4 (266B/12B active) | TP=2, both nodes | **29.79 tok/s** | 229 ms |
| Qwen3-8B (dense, BF16) | one node | 13.87 tok/s | 76 ms |

Full write-up, including the five-rung failure ladder:
[Inkling-Small on two DGX Sparks](https://blog.bytebunkerlabs.ai/posts/inkling-small-on-two-sparks/).

## Status

| phase | state |
|---|---|
| 0 — fabric | passed: 13 checks green on both nodes |
| 1 — solo | passed: Qwen3-8B, 13.87 tok/s (84% of the bandwidth ceiling) |
| 2 — solo vs TP=2 control | skipped for now; the control model's weights were lost to a cleanup bug |
| 3 — Inkling-Small-NVFP4 | **passed: 29.79 tok/s, TTFT 229 ms, KV pool 678k tokens** |
| 4 — the tuning program | next. First lever: speculative depth, capped at 1 until [vllm#48768](https://github.com/vllm-project/vllm/pull/48768) merges |

The Inkling FA4 `sm_12x` patch is vendored on demand by `scripts/fetch-inkling-mod.sh`,
pinned by commit, with a deletion trigger naming the upstream PR
([flash-attention#2348](https://github.com/Dao-AILab/flash-attention/pull/2348))
whose merge makes it garbage.
