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
| 3 | Inkling-Small-NVFP4 — 266 B total / ~12 B active, 171 GB, needs both boxes | ~55 tok/s decode with MTP; tool calls verified |
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

## Status

| piece | state |
|---|---|
| skeleton, preflight, GID resolver, model sync, bench harness | built |
| Dockerfile (NGC base tag), launch scripts (multi-node mechanics) | pending the verification pass |
| Inkling FA4 sm121 patch strategy | pending the verification pass |
