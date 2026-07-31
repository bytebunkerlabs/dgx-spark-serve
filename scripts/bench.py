#!/usr/bin/env python3
"""Single-stream serving benchmark against any OpenAI-compatible endpoint.

Stdlib only, engine-agnostic: works against vLLM, llama.cpp-server, or the
LiteLLM gateway, so baselines from the old stack and the new one land in the
same notebook. Appends one JSON line per run to bench/results.jsonl — that
file is the lab notebook; commit it.

Measures what a single user feels:
  ttft_s         time to first streamed token (prefill + queueing)
  decode_tok_s   tokens/second after the first token (the number that is
                 memory-bandwidth-bound on GB10)
  total_s        whole request

Usage:
  scripts/bench.py --url http://127.0.0.1:8000/v1 --model NAME --label phase1-baseline
  scripts/bench.py ... --runs 5 --max-tokens 256
"""
import argparse
import json
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_PROMPT = (
    "Write a detailed technical explanation of how a mixture-of-experts "
    "transformer decides which experts process each token, covering the router, "
    "top-k selection, load balancing, and shared experts. Keep going until you "
    "run out of room."
)


def one_run(url, model, prompt, max_tokens, timeout):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer none"},
    )
    t0 = time.perf_counter()
    tfirst = tlast = None
    chunks = 0
    usage = None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            ch = obj.get("choices") or []
            delta = (ch[0].get("delta") or {}) if ch else {}
            # reasoning models stream reasoning_content before content;
            # both are generated tokens and both count for timing
            if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                now = time.perf_counter()
                if tfirst is None:
                    tfirst = now
                tlast = now
                chunks += 1
    t1 = time.perf_counter()
    # usage is authoritative when the server sends it; chunk count is the
    # fallback (one token per chunk for most engines — NOT true under
    # speculative decoding, where one chunk can carry several tokens).
    toks = (usage or {}).get("completion_tokens") or chunks
    decode = None
    if tfirst is not None and tlast is not None and tlast > tfirst and toks > 1:
        decode = (toks - 1) / (tlast - tfirst)
    return {
        "ttft_s": round(tfirst - t0, 3) if tfirst else None,
        "decode_tok_s": round(decode, 2) if decode else None,
        "total_s": round(t1 - t0, 3),
        "completion_tokens": toks,
        "counted_by": "usage" if usage else "chunks",
    }


def git_rev(repo):
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True,
                    help="what this measurement is, e.g. phase2-tp2-baseline")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the unrecorded warmup request")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    out = repo / "bench" / "results.jsonl"
    out.parent.mkdir(exist_ok=True)

    if not args.no_warmup:
        print("warmup ...")
        one_run(args.url, args.model, args.prompt, args.max_tokens, args.timeout)

    rows = []
    for i in range(args.runs):
        r = one_run(args.url, args.model, args.prompt, args.max_tokens, args.timeout)
        rows.append(r)
        print(f"run {i + 1}: ttft {r['ttft_s']}s  decode {r['decode_tok_s']} tok/s  "
              f"total {r['total_s']}s  ({r['completion_tokens']} tok, by {r['counted_by']})")
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "label": args.label,
            "model": args.model,
            "url": args.url,
            "max_tokens": args.max_tokens,
            "run": i + 1,
            "git": git_rev(repo),
            **r,
        }
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")

    dec = [r["decode_tok_s"] for r in rows if r["decode_tok_s"]]
    ttft = [r["ttft_s"] for r in rows if r["ttft_s"]]
    if dec:
        print(f"\nmedian: ttft {statistics.median(ttft):.3f}s  "
              f"decode {statistics.median(dec):.2f} tok/s  -> {out.relative_to(repo)}")


if __name__ == "__main__":
    main()
