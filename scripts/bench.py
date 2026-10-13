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

  prompt_tokens  the prompt's length as the server counted it
  total_tok_s    with --concurrency N: every stream's tokens over the wall
                 time from the first request to the last answer
  peak_mem_gb    with --pid: the engine process's peak memory footprint while
                 the run went (macOS footprint, which counts Metal buffers;
                 resident memory elsewhere)

Usage:
  scripts/bench.py --url http://127.0.0.1:8000/v1 --model NAME --label phase1-baseline
  scripts/bench.py ... --runs 5 --max-tokens 256
  scripts/bench.py ... --prompt-tokens 8192 --max-tokens 64      # TTFT at 8k
  scripts/bench.py ... --concurrency 4                           # 4 streams at once
  scripts/bench.py ... --pid "$(pgrep -f llama-server)"          # and its peak memory

A long prompt is filler text sized to the token count (calibrated once against
the server's own count) behind a fresh nonce, so no run reuses another's
prompt cache.
"""
import argparse
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_PROMPT = (
    "Write a detailed technical explanation of how a mixture-of-experts "
    "transformer decides which experts process each token, covering the router, "
    "top-k selection, load balancing, and shared experts. Keep going until you "
    "run out of room."
)



def api_key():
    """The engine's key (rack init writes it; RACK_API_KEY_FILE says where), or
    a placeholder for an engine that takes none."""
    try:
        return open(os.environ["RACK_API_KEY_FILE"]).read().strip() or "none"
    except (KeyError, OSError):
        return "none"

WORDS = ("memory bandwidth decides how fast a large model writes each token because every weight "
         "must cross from memory to the compute units once per step while the arithmetic waits "
         "experts routers caches and kernels all compete for the same bytes per second").split()


def filler(n_words, seed):
    rnd = random.Random(seed)
    return " ".join(rnd.choice(WORDS) for _ in range(n_words))


def long_prompt(n_words, nonce):
    return ("[%s] Read the notes below, then answer in one paragraph: what limits decode speed?\n\n%s"
            % (nonce, filler(n_words, 7)))


class PeakMemory:
    """Sample a process's memory footprint in the background; .peak_gb when stopped."""

    def __init__(self, pid, every=1.0):
        self.pid, self.every, self.peak = int(pid), every, 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def sample(self):
        try:
            if sys.platform == "darwin":
                out = subprocess.run(["footprint", "-f", "bytes", "-p", str(self.pid)],
                                     capture_output=True, text=True, timeout=30).stdout
                m = re.search(r"Footprint:\s*(\d+)\s*B", out)
                return int(m.group(1)) if m else 0
            with open("/proc/%d/status" % self.pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return 0

    def _loop(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self.sample())
            self._stop.wait(self.every)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=60)
        self.peak = max(self.peak, self.sample())

    @property
    def peak_gb(self):
        return round(self.peak / 1e9, 2) if self.peak else None


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
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key()},
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
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "counted_by": "usage" if usage else "chunks",
        "t0": t0, "t1": t1,
    }


def streams(url, model, prompts, max_tokens, timeout):
    """One request per prompt, all at once. Returns (results, wall seconds, total tok/s)."""
    out = [None] * len(prompts)

    def go(i):
        try:
            out[i] = one_run(url, model, prompts[i], max_tokens, timeout)
        except Exception as e:   # noqa: BLE001 - one failed stream is reported, not fatal
            out[i] = {"error": str(e)[:200]}
    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(prompts))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ok = [r for r in out if r and "t0" in r]
    if not ok:
        return out, None, None
    wall = max(r["t1"] for r in ok) - min(r["t0"] for r in ok)
    toks = sum(r["completion_tokens"] or 0 for r in ok)
    return out, round(wall, 3), round(toks / wall, 2) if wall > 0 else None


def calibrate(url, model, target, timeout):
    """Words of filler that make about `target` prompt tokens, by the server's own count."""
    words = max(16, int(target * 0.75))
    for _ in range(3):
        r = one_run(url, model, long_prompt(words, "calibrate-%d" % words), 1, timeout)
        got = r.get("prompt_tokens")
        if not got:
            print("the server does not report prompt tokens: using about 0.75 words a token")
            return words
        if abs(got - target) <= max(32, target * 0.02):
            return words
        words = max(16, int(words * target / got))
    return words


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
    ap.add_argument("--prompt-tokens", type=int, default=0,
                    help="a filler prompt of about this many tokens (TTFT at 8k: --prompt-tokens 8192)")
    ap.add_argument("--concurrency", type=int, default=1, help="streams at once; reports their total tok/s")
    ap.add_argument("--pid", type=int, help="the engine's process: record its peak memory footprint")
    ap.add_argument("--note", help="free text kept with each result (quant, bpw, drafts...)")
    ap.add_argument("--out", help="where results go (default: bench/results.jsonl, the notebook)")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve() if args.out else repo / "bench" / "results.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    words = calibrate(args.url, args.model, args.prompt_tokens, args.timeout) if args.prompt_tokens else 0

    def prompt(tag):
        return long_prompt(words, "%s-%d" % (tag, random.getrandbits(48))) if words else args.prompt

    if not args.no_warmup:
        print("warmup ...")
        one_run(args.url, args.model, prompt("warmup"), args.max_tokens, args.timeout)

    rows = []
    for i in range(args.runs):
        mem = PeakMemory(args.pid) if args.pid else None
        if mem:
            mem.__enter__()
        try:
            res, wall, total = streams(args.url, args.model, [prompt("run%d-%d" % (i, k))
                                                              for k in range(max(1, args.concurrency))],
                                       args.max_tokens, args.timeout)
        finally:
            if mem:
                mem.__exit__()
        bad = [r for r in res if "error" in r]
        if bad:
            print(f"run {i + 1}: {len(bad)} of {len(res)} streams failed: {bad[0]['error']}")
        ok = [r for r in res if "t0" in r]
        if not ok:
            continue
        # one stream: its own numbers; several: the median stream, and their total
        r = dict(sorted(ok, key=lambda x: x["decode_tok_s"] or 0)[len(ok) // 2])
        r.pop("t0"), r.pop("t1")
        r.update(concurrency=args.concurrency, total_tok_s=total if args.concurrency > 1 else r["decode_tok_s"],
                 wall_s=wall, peak_mem_gb=mem.peak_gb if mem else None)
        rows.append(r)
        print(f"run {i + 1}: ttft {r['ttft_s']}s  decode {r['decode_tok_s']} tok/s  "
              f"total {r['total_s']}s  ({r['completion_tokens']} tok, by {r['counted_by']})"
              + (f"  prompt {r['prompt_tokens']} tok" if r.get("prompt_tokens") else "")
              + (f"  {args.concurrency} streams {total} tok/s" if args.concurrency > 1 else "")
              + (f"  peak {r['peak_mem_gb']} GB" if r.get("peak_mem_gb") else ""))
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "label": args.label,
            "model": args.model,
            "url": args.url,
            "max_tokens": args.max_tokens,
            "prompt_target": args.prompt_tokens or None,
            "run": i + 1,
            "git": git_rev(repo),
            "note": args.note,
            **r,
        }
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")

    med = lambda k: statistics.median([r[k] for r in rows if r.get(k)]) if any(r.get(k) for r in rows) else None
    if rows:
        print(f"\nmedian: ttft {med('ttft_s')}s  decode {med('decode_tok_s')} tok/s"
              + (f"  {args.concurrency} streams {med('total_tok_s')} tok/s" if args.concurrency > 1 else "")
              + (f"  peak {max(r['peak_mem_gb'] or 0 for r in rows)} GB" if args.pid else "")
              + f"  -> {os.path.relpath(out, repo)}")


if __name__ == "__main__":
    main()
