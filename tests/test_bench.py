"""scripts/bench.py against a fake OpenAI-compatible server: prompt length,
concurrent streams, and results that land where they are told."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Fake(BaseHTTPRequestHandler):
    """Streams max_tokens tokens, 5 ms apart; counts a prompt's tokens as its words."""
    seen = []
    drift = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        Fake.seen.append(prompt)
        n = body["max_tokens"]
        if not body.get("stream"):           # greedy.py: one answer, a word of it changed when asked
            text = "answer to " + prompt + (" (drifted)" if Fake.drift and "Fibonacci" in prompt else "")
            d = {"choices": [{"message": {"content": text}}], "usage": {"completion_tokens": 3}}
            raw = json.dumps(d).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(0.02)
        for i in range(n):
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"t"}}]}\n\n')
            self.wfile.flush()
            time.sleep(0.005)
        usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": n}
        self.wfile.write(("data: %s\n\n" % json.dumps({"choices": [], "usage": usage})).encode())
        self.wfile.write(b"data: [DONE]\n\n")


class Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def bench(self, *extra):
        out = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "bench.py"), "--url", self.url,
                            "--model", "m", "--label", "t", "--runs", "2", "--max-tokens", "20", "--out", out] + list(extra),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(l) for l in open(out)], r.stdout

    def test_a_long_prompt_is_the_size_asked_and_never_repeats(self):
        Fake.seen = []
        rows, _ = self.bench("--prompt-tokens", "2000")
        for r in rows:
            self.assertLess(abs(r["prompt_tokens"] - 2000), 60, r)
            self.assertEqual(r["prompt_target"], 2000)
        firsts = [p.split("]")[0] for p in Fake.seen[-3:]]          # warmup and two runs
        self.assertEqual(len(set(firsts)), 3)                        # a fresh nonce each: no prompt cache hits

    def test_concurrent_streams_report_their_total(self):
        rows, out = self.bench("--concurrency", "4", "--note", "q4")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual((r["concurrency"], r["note"]), (4, "q4"))
            self.assertGreater(r["total_tok_s"], r["decode_tok_s"] * 2)   # four streams at once
        self.assertIn("4 streams", out)

    def test_peak_memory_of_a_process(self):
        rows, _ = self.bench("--pid", str(os.getpid()))
        self.assertTrue(all(r["peak_mem_gb"] and r["peak_mem_gb"] > 0 for r in rows), rows)


    def test_greedy_outputs_compare_byte_for_byte(self):
        d = tempfile.mkdtemp()
        run = lambda label: subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "greedy.py"), "--url", self.url,
                                            "--model", "m", "--label", label, "--dir", d],
                                           capture_output=True, text=True, timeout=120)
        cmp = lambda a, b: subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "greedy.py"),
                                           "--compare", a, b, "--dir", d], capture_output=True, text=True)
        Fake.drift = False
        run("off"), run("on")
        Fake.drift = True
        run("drifted")
        Fake.drift = False
        same = cmp("off", "on")
        self.assertEqual(same.returncode, 0)
        self.assertIn("20 of 20 identical", same.stdout)
        diff = cmp("off", "drifted")
        self.assertEqual(diff.returncode, 1)
        self.assertIn("19 of 20 identical", diff.stdout)
        self.assertIn("first difference: prompt 1", diff.stdout)


if __name__ == "__main__":
    unittest.main()
