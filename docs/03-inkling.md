# 03 — Phase 3: Inkling-Small-NVFP4, the capstone

276B-class total, ~12B active, 170.7 GB on disk. ~85 GB of weights per node at
TP=2 — the model that *needs* both boxes. NVFP4 is hardware-native on GB10.

## Why this one is genuinely hard, in one paragraph

Inkling support entered vLLM on 2026-07-16 and first shipped stable in
**v0.26.0 (2026-07-27)** — so the NGC image (vLLM 0.24) is out, which is why
this phase uses `--profile upstream`. And even v0.26.0 cannot boot it on GB10:
Inkling's FA4 attention needs an SM120 paged-KV kernel that exists only in
open PRs (`flash-attention#2348`; the vLLM-side fix #49681 was closed
unmerged). Every rank dies in warmup with `Paged KV not supported on SM 12.0`.
The fix is a three-edit, AST-validated patch applied inside each container at
launch — vendored, pinned, and deletable the day upstream lands it.

## Honest expectations

**No published tok/s number exists for this model on Sparks** (as of
2026-07-30). The ~12B-active NVFP4 comparables on two Sparks: **~20–25 tok/s
without effective speculation, 40–65 with**. Inkling's MTP is capped at
`num_speculative_tokens: 1` until vllm#48768 merges, so start expectations at
the low end — and know that the first properly measured number is publishable.

Known-broken until further notice: tool calling (parser fix #50403 merged
2026-07-30, nightly-only). Test it, don't assume it.

## The run

```bash
# once: image + patch + weights
scripts/build.sh --profile upstream
scripts/fetch-inkling-mod.sh                       # vendored, pinned, licensed
hf download thinkingmachines/Inkling-Small-NVFP4   # 170.7 GB — the long pole
scripts/sync-model.sh thinkingmachines/Inkling-Small-NVFP4

# every launch: this model wants the whole machine
scripts/stop-cluster.sh                            # and anything else serving
# free ~115 GiB per node — your runbook §02 (stop stack, drop caches, earlyoom off)
scripts/preflight.sh                               # on both

scripts/launch-cluster.sh recipes/inkling-small-nvfp4.env
```

First boot is the slow one: ~171 GB streamed from disk per node pair plus
kernel compilation. `VLLM_USE_AOT_COMPILE=1` caches artifacts, so later boots
are much faster.

**Gates, in order:**

1. mod applied on both nodes without error (it AST-validates before touching
   anything, and refuses rather than mispatches)
2. `Application startup complete.` with weights split ~85 GB/node
3. a completion returns; `bench.py --label phase3-inkling-baseline` recorded
4. a tool-call round trip — *expected flaky*; log what you see

## The fallback ladder (memory pressure is the boss fight)

| symptom | move |
|---|---|
| startup dies capturing CUDA graphs | recipe's compilation-config → `{"cudagraph_mode":"PIECEWISE"}` — FULL variants can exhaust unified memory on cross-node TP |
| still dying | `--enforce-eager` as a diagnostic — slow, but isolates graphs vs everything else |
| load stalls near the end | page cache crowding — drop caches on both, retry; consider a 60 s drop-caches loop during load |
| node wedges (not clean OOM) | that's unified-memory pressure, not a bug to debug live — lower `--max-model-len` or `--max-num-seqs`, never raise `--gpu-memory-utilization` past 0.8 |
| `LAMPORT` / reduce-scatter errors | `LAMPORT_RS_SCONV=0` missing — it's mandatory on RoCE (the fused path assumes NVLink) |

## Deletion triggers — write the date in when they fire

- `mods/inkling-sm12-paged-kv` → delete when flash-attention#2348 reaches
  vLLM's pinned FA4 fork
- `num_speculative_tokens: 1` cap → raise and re-bench when vllm#48768 merges
  (this is the single biggest expected speed unlock)
- tool-parser caveat → drop when the first stable release after 2026-07-30
  ships #50403
