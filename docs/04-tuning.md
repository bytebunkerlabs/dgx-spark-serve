# 04 — The performance program

The goal after phase 3 is simple to state: **more tokens per second, measured, forever.**
This doc is the method and the ledger of levers.

## The loop

1. `scripts/bench.py --label <phase>-baseline` against the running config.
2. Change **one** lever.
3. Bench again, same label scheme: `<phase>-<lever>`.
4. Commit — config change and both numbers in the message. No number, no merge.
5. If it didn't move, revert. `bench/results.jsonl` keeps the negative result — those are
   worth as much as wins (the 0.15% fabric-doubling null result is why this repo exists).

Single-stream decode tok/s is the headline number (that's what a user feels), but record the
concurrency sweep too once a config stabilizes: sweep `--max-num-seqs` and concurrent clients
until aggregate saturates — that's the knee that sizes the agent swarm.

## The levers

Status: **[measured-here]** = numbers from this cluster. **[reported]** = credible external
number for GB10. **[pending]** = to be verified in the phase 4 pass.

| lever | what it does | expect | status |
|---|---|---|---|
| MTP / speculative decoding | model drafts k tokens, verifies in one pass; decode cost amortizes | biggest single win on bandwidth-bound decode. DSpark: 3 draft tokens, ~60% acceptance. Inkling: ~40% acceptance reported | [measured-here] / [reported] |
| CUDA graph mode | removes per-step launch overhead | FULL_AND_PIECEWISE fastest; FULL variants can exhaust GB10 memory on cross-node TP — PIECEWISE is the documented fallback | [reported] |
| KV-cache quantization (NVFP4/FP8 KV) | shrinks per-token cache | buys context length, not speed: it's how 1M ctx fits (DSpark: 2.6M-token pool in ~18 GiB/node) | [measured-here] |
| load format (fastsafetensors / instanttensor) | parallel weight load path | minutes off every boot; matters for iteration speed, not serving | [reported] |
| `gpu-memory-utilization` | fraction of the unified pool vLLM claims | 0.845 proven on DSpark at 1M ctx; higher steals from the OS/page cache and invites the OOM-with-memory-free trap | [measured-here] |
| chunked prefill + prefix caching | overlaps prefill with decode; reuses shared prefixes | TTFT and multi-turn wins; measure with agent-shaped workloads, not one-shots | [pending] |
| `--async-scheduling` | overlaps CPU scheduling with GPU work | small steady decode win reported on Spark builds | [pending] |
| `max-num-seqs` | concurrency ceiling | single-stream vs aggregate trade: DSpark 66 tok/s @1 → ~150 tok/s @6 | [measured-here] |
| NCCL topology | which rail(s), GID resolution | decode is latency-bound: both-rails moved decode 0.15% (null). Correct GID + `NET/IB` is mandatory; second rail is for staging | [measured-here] |
| batch-invariant kernels | determinism vs speed | check what Inkling's stack pins before assuming kernels are swappable | [pending] |

## Known baselines

| model | active | quant | nodes | decode tok/s | source |
|---|---|---|---|---|---|
| DeepSeek-V4-Flash-DSpark | 13 B | FP4/FP8 | 2 | 66 single / ~150 @6 | this cluster, 2026-07 |
| DeepSeek-V4-Flash-DSpark | 13 B | FP4/FP8 + NVFP4 KV | 2 | 35–55 by workload; 55.17 best (code, thinking off, 40.4% MTP acceptance) | classmethod.jp, 2026-07 |
| gpt-oss-120b | ~5 B | MXFP4 | 1 | ~60 | eugr changelog |
| Qwen3.6-35B-A3B | ~3 B | FP8 → NVFP4+MTP | 1 | 21 → ~102 | rikkarth / vlaicu — the MTP+NVFP4 case study |
| Nemotron-3-Super-120B | ~12 B | NVFP4 | 1 | 22.7–23.7 | vLLM official Spark blog |
| Inkling-Small-NVFP4 | ~12 B | NVFP4 | 2 | **no published number exists** (2026-07-30) — comparables say ~20–25 without effective MTP, 40–65 stretch; k=1 cap until vllm#48768 | phase 3 measures it |

An earlier draft of this file attributed 55.17 tok/s to Inkling-Small. It belongs to
DeepSeek-V4-Flash-DSpark. Fill this table only with numbers that have a `results.jsonl` line or
a cited source — a number without provenance is a rumor, and the rumor was us, once, already.

Two workload effects bigger than most levers: thinking mode on/off moved DSpark decode 1.32×
(41.9 → 55.2), and MTP acceptance swung 24.2% → 40.4% with it. Pin thinking mode in benchmarks,
and always log acceptance — it's the most workload-sensitive number in the whole system.

## The heavier harness, when bench.py isn't enough

`bench.py` is the always-on notebook line. For real sweeps, the serving container already
carries the standard harness — zero extra deps:

```bash
docker exec serve_node vllm bench serve \
  --backend openai-chat --base-url http://localhost:8000 --endpoint /v1/chat/completions \
  --model MODEL --dataset-name random \
  --random-input-len 512 --random-output-len 256 --random-range-ratio 0.1 \
  --num-prompts 8 --num-warmups 2 --max-concurrency 1 --ignore-eos \
  --percentile-metrics ttft,tpot,itl,e2el --save-result
# then sweep --max-concurrency 2 4 (num_prompts = 4×C); weekly: --random-input-len 65536
```

Protocol notes that keep numbers honest: `--ignore-eos` + random prompts defeat prefix-cache
flattery; single-stream decode tok/s ≈ 1000 / TPOT-p50-ms; never compare across vLLM image tags
without saying so; JIT warmup means the first request after boot is always garbage.

## Where the ceiling is

GB10 decode is memory-bandwidth-bound: ~273 GB/s LPDDR5x per node. Per token, a TP=2 MoE reads
`(active_params × bpw ÷ 8) ÷ 2` per node plus KV; the ceiling is that against 273 GB/s, and MTP
multiplies effective tokens per read. Everything in the lever table either (a) reads fewer bytes
per token, (b) reads them less often, or (c) hides non-read time. If a proposed tweak does none
of those three, it will not move decode — bench it anyway, then revert it.
