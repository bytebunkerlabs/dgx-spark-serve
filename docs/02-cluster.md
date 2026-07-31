# 02 — Phase 2: the distribution experiment

Goal: the same weights served solo and TP=2, benched both ways. The delta is
the measured cost (or win) of distribution on this fabric — not folklore.

gpt-oss-120b is chosen *because* it fits on one node (~62 GB MXFP4): that's
what makes the comparison possible at all. Community reference points:
~60 tok/s solo, "50–100" two-node.

Two recipes, one variable: `phase2-gpt-oss-120b-solo.env` and
`phase2-gpt-oss-120b.env` differ only in `--tensor-parallel-size 2`.

**On spark-1:**

```bash
# A. solo leg
scripts/launch-solo.sh recipes/phase2-gpt-oss-120b-solo.env
scripts/bench.py --model openai/gpt-oss-120b --label phase2-solo
scripts/stop-cluster.sh

# B. cluster leg — weights must exist on BOTH nodes first
scripts/sync-model.sh openai/gpt-oss-120b
scripts/launch-cluster.sh recipes/phase2-gpt-oss-120b.env --debug
```

`--debug` turns on `NCCL_DEBUG=INFO` for the first run. **The gate is a log
line, not a feeling:**

```
NCCL INFO ... NET/IB      ← RDMA. Real.
NCCL INFO ... NET/Socket  ← TCP fallback. STOP — every number after this is invalid.
```

Also watch for `Model loading took ~31 GiB` per node — half the weights on
each box is TP working.

```bash
scripts/bench.py --model openai/gpt-oss-120b --label phase2-tp2
```

Compare `phase2-solo` vs `phase2-tp2` in the notebook. Expect single-stream to
be similar or modestly different — your own build log showed decode
all-reduces are tens of KB and latency-bound. Now you'll have it for vLLM TP,
on your own stack, with provenance.

## When it breaks

| symptom | cause | fix |
|---|---|---|
| abort: "vLLM lacks --nnodes" | NGC image (vLLM 0.24) predates native multi-node serve | `scripts/build.sh --profile upstream`, or NVIDIA's Ray playbook path |
| `NET/Socket` in logs | RDMA not reachable: `NCCL_IB_HCA` unset/wrong | check `IB_HCAS` lists **both** twins; container runs `--privileged` via our script |
| hangs at NCCL init | one rail firewalled | preflight's ufw check; `allow in on` *both* fabric interfaces |
| plateau ~100 Gb/s in nccl-tests | one RoCE twin in `NCCL_IB_HCA` | both: `rocep1s0f0,roceP2p1s0f0` |
| `ibv_modify_qp … 61 No data available` | a *stored* GID index somewhere | delete it; NCCL ≥ 2.21 auto-selects (docs/00) |
| worker looks silent | it logs on its own box | `ssh spark-2 docker logs -f serve_node` |
| `No available shared memory broadcast block found in 60 seconds` | one rank compiling while the other waits | benign; wait |
| multi-node hang, pip-wheel image | bundled pip NCCL (vllm#42354) | our Dockerfile's NCCL redirect guard handles it — verify the build log printed `nccl: redirected` |
