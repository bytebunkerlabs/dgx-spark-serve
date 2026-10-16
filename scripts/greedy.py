#!/usr/bin/env python3
"""Same prompts, temperature 0, outputs kept: are two setups byte-identical?

Speculative decoding is only lossless if, at temperature 0, it produces exactly
what the engine produces without it. This runs 20 fixed prompts (or --prompts
FILE, one per line) against an OpenAI-compatible endpoint, greedy, and writes
each answer (content and reasoning) to bench/outputs/<label>.jsonl. Then
--compare A B says how many are identical and shows the first difference.

    scripts/greedy.py --url http://127.0.0.1:8888/v1 --model NAME --label tf-drafts-off
    scripts/greedy.py --compare tf-drafts-off tf-drafts-on

Stdlib only.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

PROMPTS = [
    "Explain in three sentences why the sky is blue.",
    "Write a Python function that returns the n-th Fibonacci number iteratively.",
    "List the planets of the solar system in order from the sun.",
    "Translate into French: The engine serves one model per machine.",
    "What is 17 multiplied by 23? Show the steps.",
    "Summarize the causes of the French Revolution in one paragraph.",
    "Write a haiku about memory bandwidth.",
    "Give a JSON object with keys name, port and enabled for a web server on port 8080.",
    "What does the Unix command `tar -xzf file.tar.gz` do?",
    "Explain the difference between a process and a thread.",
    "Write a SQL query that counts orders per customer from a table orders(customer_id, id).",
    "Name three sorting algorithms and their average time complexity.",
    "Describe how a hash table handles collisions.",
    "Write a bash one-liner that prints the 5 largest files in the current directory.",
    "What is the capital of Australia, and why is it not Sydney?",
    "Explain mixture-of-experts routing to a high-school student.",
    "Write a regular expression that matches an IPv4 address.",
    "What are the first ten prime numbers?",
    "Rewrite this sentence in the passive voice: The team shipped the release on Friday.",
    "Explain what a KV cache is in transformer inference, in four sentences.",
]


def api_key():
    try:
        return open(os.environ["RACK_API_KEY_FILE"]).read().strip() or "none"
    except (KeyError, OSError):
        return "none"


def ask(url, model, prompt, max_tokens, timeout, extra):
    body = dict({"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
                 "temperature": 0, "top_p": 1, "seed": 0, "stream": False}, **extra)
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    m = (d.get("choices") or [{}])[0].get("message") or {}
    return {"content": m.get("content") or "", "reasoning": m.get("reasoning_content") or m.get("reasoning") or "",
            "tokens": (d.get("usage") or {}).get("completion_tokens")}


def digest(r):
    return hashlib.sha256((r["reasoning"] + "\x00" + r["content"]).encode("utf-8")).hexdigest()[:16]


def outputs_dir():
    return Path(__file__).resolve().parent.parent / "bench" / "outputs"


def load(label, d=None):
    path = Path(d or outputs_dir()) / (label + ".jsonl")
    return [json.loads(l) for l in open(path)]


def compare(a, b, d=None):
    ra, rb = load(a, d), load(b, d)
    same = 0
    first = None
    for x, y in zip(ra, rb):
        if x["sha"] == y["sha"]:
            same += 1
        elif first is None:
            ta, tb = x["reasoning"] + x["content"], y["reasoning"] + y["content"]
            i = next((k for k in range(min(len(ta), len(tb))) if ta[k] != tb[k]), min(len(ta), len(tb)))
            first = (x["i"], i, ta[max(0, i - 40):i + 40], tb[max(0, i - 40):i + 40])
    n = min(len(ra), len(rb))
    print("%d of %d identical (%s vs %s)" % (same, n, a, b))
    if first:
        print("first difference: prompt %d at character %d\n  %s: %r\n  %s: %r" % (first[0], first[1], a, first[2], b, first[3]))
    return same == n and len(ra) == len(rb)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8888/v1")
    ap.add_argument("--model")
    ap.add_argument("--label")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--prompts", help="a file of prompts, one per line (default: the 20 built in)")
    ap.add_argument("--no-thinking", action="store_true", help="send chat_template_kwargs.enable_thinking=false")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--dir", help="where outputs live (default: bench/outputs)")
    a = ap.parse_args(argv)
    if a.compare:
        return 0 if compare(a.compare[0], a.compare[1], a.dir) else 1
    if not (a.model and a.label):
        ap.error("--model and --label (or --compare A B)")
    prompts = [l.rstrip("\n") for l in open(a.prompts) if l.strip()] if a.prompts else PROMPTS
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if a.no_thinking else {}
    d = Path(a.dir or outputs_dir())
    d.mkdir(parents=True, exist_ok=True)
    path = d / (a.label + ".jsonl")
    with open(path, "w") as f:
        for i, p in enumerate(prompts):
            r = ask(a.url, a.model, p, a.max_tokens, a.timeout, extra)
            rec = dict(r, i=i, prompt=p, sha=digest(r))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print("%2d  %s  %s tok" % (i, rec["sha"], r["tokens"]))
    print("-> %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
