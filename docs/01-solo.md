# 01 — Phase 1: solo

Goal: our image serves a small model on spark-1, end to end. This proves the
container, the engine, GPU access, and the API — with zero distribution to blame
when something breaks.

**On spark-1:**

```bash
scripts/preflight.sh                      # gate 0 — must be all-PASS
scripts/build.sh --profile ngc            # build + sync (sync harmless here)
scripts/launch-solo.sh recipes/phase1-qwen3-8b.env
```

First boot compiles kernels — minutes, not seconds. You're waiting for:

```
Application startup complete.
```

**Gate — in a second terminal:**

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Reply with exactly: SOLO OK"}],"max_tokens":8,"temperature":0}'

# then the baseline — the first line in the lab notebook:
scripts/bench.py --model Qwen/Qwen3-8B --label phase1-solo-baseline
```

Commit `bench/results.jsonl`. Phase 1 is done.

## When it breaks

| symptom | cause | fix |
|---|---|---|
| model download instead of instant load | weights not in `~/dgx/hf` | pre-stage, or let it download once (small model) |
| OOM / wedge during load | pool already occupied | your runbook §02: stop stack, `drop_caches`, want ~115 GiB free |
| `nvidia-smi` shows no memory numbers | unified memory — normal | `free -h` is the truth |
| first request takes ~30 s | JIT warmup | always fire a throwaway request before judging anything |
