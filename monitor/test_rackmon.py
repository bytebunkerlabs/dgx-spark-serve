"""Unit tests for rackmon.py. Standard library only; runs anywhere:

    python3 -m unittest monitor/test_rackmon.py -v
"""
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rackmon  # noqa: E402

STAT_A = """cpu  100 0 100 800 0 0 0 0 0 0
cpu0 50 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 400 0 0 0 0 0 0
intr 1 2 3
"""
STAT_B = """cpu  200 0 150 850 0 0 0 0 0 0
cpu0 140 0 60 400 0 0 0 0 0 0
cpu1 60 0 90 450 0 0 0 0 0 0
"""
MEMINFO = """MemTotal:       127600812 kB
MemFree:        120505356 kB
MemAvailable:   119175496 kB
Cached:           2000000 kB
SwapTotal:       16777212 kB
SwapFree:        16777212 kB
HugePages_Total:       0
"""
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0
enp1s0f0np0: 5000000 100 0 0 0 0 0 0 7000000 90 0 0 0 0 0 0
tailscale0: 300 3 0 0 0 0 0 0 400 4 0 0 0 0 0 0
"""
# what a GB10 answers: unified memory, so the memory fields are [N/A]
GB10_FIELDS = rackmon.Node.GPU_BASE + ["clocks_event_reasons.active"]
GB10_CSV = ("0, NVIDIA GB10, GPU-24d24f17-5af5-f99f-ae1f-b6010fc276df, 37, 51, 41.27, 2411, 3003, P0, "
            "[N/A], [N/A], 580.159.03, 0x0000000000000000\n")
VLLM = """# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="dsv4"} 2.0
vllm:num_requests_waiting{engine="0",model_name="dsv4"} 1.0
vllm:kv_cache_usage_perc{engine="0",model_name="dsv4"} 0.4125
vllm:prefix_cache_queries_total{engine="0",model_name="dsv4"} 1000.0
vllm:prefix_cache_hits_total{engine="0",model_name="dsv4"} 250.0
vllm:num_preemptions_total{engine="0",model_name="dsv4"} 3.0
vllm:prompt_tokens_total{engine="0",model_name="dsv4"} 50000.0
vllm:generation_tokens_total{engine="0",model_name="dsv4"} 12000.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="dsv4"} 40.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="dsv4"} 2.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.1",model_name="dsv4"} 10.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.5",model_name="dsv4"} 30.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="1.0",model_name="dsv4"} 40.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf",model_name="dsv4"} 42.0
vllm:time_to_first_token_seconds_count{engine="0",model_name="dsv4"} 42.0
vllm:time_to_first_token_seconds_sum{engine="0",model_name="dsv4"} 13.5
vllm:e2e_request_latency_seconds_count{engine="0",model_name="dsv4"} 42.0
vllm:e2e_request_latency_seconds_sum{engine="0",model_name="dsv4"} 420.0
vllm:inter_token_latency_seconds_count{engine="0",model_name="dsv4"} 12000.0
vllm:inter_token_latency_seconds_sum{engine="0",model_name="dsv4"} 444.0
vllm:cache_config_info{block_size="16",cache_dtype="auto",num_gpu_blocks="20000",prefix_caching="True"} 1.0
process_resident_memory_bytes 1.2e+09
python_gc_objects_collected_total{generation="0"} NaN
"""


class Parsers(unittest.TestCase):
    def test_cpu(self):
        a, b = rackmon.parse_proc_stat(STAT_A), rackmon.parse_proc_stat(STAT_B)
        self.assertEqual(a["cpu"], (800, 1000))
        # total: +200 jiffies, idle +50 -> 75 % busy
        self.assertEqual(rackmon.busy_pct(a["cpu"], b["cpu"]), 75.0)
        self.assertEqual(rackmon.busy_pct(a["cpu0"], b["cpu0"]), 100.0)
        self.assertIsNone(rackmon.busy_pct(None, b["cpu"]))
        self.assertIsNone(rackmon.busy_pct(b["cpu"], b["cpu"]))   # no time passed

    def test_meminfo(self):
        m = rackmon.parse_meminfo(MEMINFO)
        self.assertEqual(m["MemTotal"], 127600812 * 1024)
        self.assertEqual(m["HugePages_Total"], 0)                  # bare count, not scaled

    def test_net_dev(self):
        n = rackmon.parse_net_dev(NETDEV)
        self.assertEqual(n["enp1s0f0np0"], (5000000, 7000000))
        self.assertIn("lo", n)

    def test_nvidia_gb10(self):
        rows = rackmon.parse_csv(GB10_CSV, GB10_FIELDS)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["name"], "NVIDIA GB10")
        self.assertEqual(rackmon.num(r["utilization.gpu"]), 37)
        self.assertEqual(rackmon.num(r["power.draw"]), 41.27)
        self.assertIsNone(rackmon.num(r["memory.total"]))           # [N/A] on unified memory
        self.assertEqual(rackmon.decode_throttle(r["clocks_event_reasons.active"]), [])

    def test_num(self):
        for na in ("[N/A]", "N/A", "[Not Supported]", "", None, "nan", "inf"):
            self.assertIsNone(rackmon.num(na), na)
        self.assertEqual(rackmon.num("2411"), 2411)
        self.assertEqual(rackmon.num("10.5"), 10.5)

    def test_throttle(self):
        self.assertEqual(rackmon.decode_throttle("0x0000000000000001"), [])       # idle is not throttling
        self.assertEqual(rackmon.decode_throttle("0x4"), ["power cap"])
        self.assertEqual(rackmon.decode_throttle("0x60"), ["thermal", "hw thermal"])
        self.assertIsNone(rackmon.decode_throttle("[N/A]"))

    def test_prom_and_vllm(self):
        samples = rackmon.parse_prom(VLLM)
        names = {n for n, _, _ in samples}
        self.assertNotIn("python_gc_objects_collected_total", names)              # NaN dropped
        self.assertEqual(rackmon.engine_kind(VLLM), "vllm")
        s = rackmon.summarize_engine("vllm", samples)
        self.assertEqual(s["models"], ["dsv4"])
        self.assertEqual((s["running"], s["waiting"]), (2, 1))
        self.assertEqual(s["kv_pct"], 41.2)
        self.assertEqual(s["requests_ok"], 42.0)
        self.assertEqual(s["kv_tokens"], 320000)
        self.assertEqual(s["ttft"]["count"], 42.0)
        self.assertEqual(s["itl"]["sum"], 444.0)

    def test_engine_kinds(self):
        self.assertEqual(rackmon.engine_kind("sglang:num_running_reqs 1\n"), "sglang")
        self.assertEqual(rackmon.engine_kind("# HELP x\nllamacpp:prompt_tokens_total 5\n"), "llama.cpp")
        self.assertIsNone(rackmon.engine_kind("litellm_requests_total 4\nnode_load1 0.1\n"))

    def test_quantile(self):
        b = {0.1: 10.0, 0.5: 30.0, 1.0: 40.0, float("inf"): 42.0}
        self.assertAlmostEqual(rackmon.hist_quantile(0.5, b), 0.5 * 0 + 0.1 + (0.4 * (21 - 10) / 20), places=6)
        self.assertEqual(rackmon.hist_quantile(0.99, b), 1.0)                     # lands in +Inf: last bound
        prev = dict(b)
        self.assertIsNone(rackmon.hist_quantile(0.5, b, prev))                     # nothing new in the window
        cur = {0.1: 10.0, 0.5: 34.0, 1.0: 44.0, float("inf"): 46.0}
        # 4 new requests, all between 0.1 and 0.5 s
        self.assertTrue(0.1 < rackmon.hist_quantile(0.5, cur, prev) <= 0.5)

    def test_engine_window(self):
        e = rackmon.Engine("127.0.0.1", 8888, "vllm")
        s0 = rackmon.summarize_engine("vllm", rackmon.parse_prom(VLLM))
        s1 = dict(s0, gen_total=s0["gen_total"] + 270.0, prompt_total=s0["prompt_total"] + 5000.0,
                  prefix_queries=1100.0, prefix_hits=300.0)
        s1["ttft"] = {"sum": 15.5, "count": 46.0,
                      "buckets": {0.1: 10.0, 0.5: 34.0, 1.0: 44.0, float("inf"): 46.0}}
        e.hist.append((100.0, s0))
        e.hist.append((110.0, s1))
        w = e.window()
        self.assertEqual(w["gen_tps"], 27.0)
        self.assertEqual(w["prompt_tps"], 500.0)
        self.assertEqual(w["prefix_hit_pct"], 50.0)
        self.assertIsNotNone(w["ttft_p50"])
        snap = e.snapshot()
        self.assertNotIn("ttft", snap)                    # buckets stay on the node
        self.assertEqual(snap["models"], ["dsv4"])

    def test_listen(self):
        tcp = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
               "   0: 00000000:22B8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1\n"
               "   1: 0100007F:0FA0 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1\n"
               "   2: BA19A8C0:0FA0 0119A8C0:D431 01 00000000:00000000 00:00000000 00000000     0        0 3 1\n")
        self.assertEqual(rackmon.parse_listen(tcp), [("0.0.0.0", 8888), ("127.0.0.1", 4000)])
        tcp6 = ("  sl  local_address                         remote_address                        st\n"
                "   0: 00000000000000000000000000000000:2B67 00000000000000000000000000000000:0000 0A 0 0 0\n")
        self.assertEqual(rackmon.parse_listen(tcp6, v6=True), [("::", 11111)])

    def test_cpu_model(self):
        arm = "processor\t: 0\nCPU implementer\t: 0x41\nCPU part\t: 0xd85\n\nprocessor\t: 1\nCPU implementer\t: 0x41\nCPU part\t: 0xd87\n\nprocessor\t: 2\nCPU part\t: 0xd85\n"
        self.assertEqual(rackmon.parse_cpu_model(arm), "2x Cortex-X925 + 1x Cortex-A725")
        spark = "".join("processor\t: %d\nCPU part\t: %s\n\n" % (i, "0xd87" if i % 2 else "0xd85") for i in range(4))
        self.assertEqual(rackmon.parse_cpu_model(spark), "2x Cortex-X925 + 2x Cortex-A725")   # tie: big cores first
        x86 = "processor\t: 0\nmodel name\t: AMD Ryzen 9 7950X 16-Core Processor\n\nprocessor\t: 1\nmodel name\t: AMD Ryzen 9 7950X 16-Core Processor\n"
        self.assertEqual(rackmon.parse_cpu_model(x86), "AMD Ryzen 9 7950X 16-Core Processor")

    def test_os_release(self):
        self.assertEqual(rackmon.parse_os_release('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.4 LTS"\n'), "Ubuntu 24.04.4 LTS")

    def test_container_and_labels(self):
        cid = "00a0a94d5ff699a75bfa54dee15066c1270d8441a458f02ed72aa71b70082346"
        self.assertEqual(rackmon.container_of("0::/system.slice/docker-%s.scope\n" % cid), cid)
        self.assertEqual(rackmon.container_of("0::/docker/%s\n" % cid), cid)
        self.assertIsNone(rackmon.container_of("0::/user.slice/user-1000.slice/session-3.scope\n"))
        cmd = "python3\0-m\0vllm.entrypoints.openai.api_server\0--api-key\0sk-SECRET\0"
        self.assertEqual(rackmon.proc_label(cmd, "/usr/bin/python3"), "vllm")
        self.assertEqual(rackmon.proc_label("", "/usr/libexec/gnome-remote-desktop-daemon"), "gnome-remote-desktop-daemon")
        self.assertEqual(rackmon.proc_label("/opt/app/server\0--token\0abc\0", ""), "server")

    def test_slim_container_drops_secrets(self):
        raw = {"Id": "abc", "Names": ["/serve_node"], "Image": "vllm/vllm-openai:x", "State": "running",
               "Status": "Up 2 hours", "Created": 1, "Command": "vllm serve --api-key sk-SECRET",
               "Labels": {"com.docker.compose.project": "dgx-inference", "secret": "x"},
               "Env": ["HF_TOKEN=hf_SECRET"]}
        out = rackmon.slim_container(raw)
        self.assertEqual(out["name"], "serve_node")
        self.assertEqual(out["project"], "dgx-inference")
        self.assertNotIn("SECRET", json.dumps(out))


class FakeNode:
    """A Node that answers canned snapshots, for the HTTP layer."""

    def __init__(self, name, role="head"):
        self.name, self.role = name, role

    def snapshot(self, history=0):
        d = {"schema": rackmon.SCHEMA, "name": self.name, "role": self.role, "ok": True, "gpus": [], "engines": []}
        if history:
            d["history"] = {"t": [1.0] * history, "cpu": [5.0] * history}
        return d


def serve_app(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), rackmon.make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Http(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        os.environ.pop("MONITOR_PEERS", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_auth(self):
        srv, url = serve_app(rackmon.App(FakeNode("spark-1"), "tok-123"))
        try:
            code, hello = get(url + "/v1/hello")
            self.assertEqual((code, hello["service"], hello["name"]), (200, "rack-monitor", "spark-1"))
            self.assertEqual(get(url + "/v1/node")[0], 401)
            self.assertEqual(get(url + "/v1/node", {"Authorization": "Bearer nope"})[0], 401)
            code, node = get(url + "/v1/node?history=3", {"Authorization": "Bearer tok-123"})
            self.assertEqual((code, len(node["history"]["t"])), (200, 3))
            basic = base64.b64encode(b"rack:tok-123").decode()
            self.assertEqual(get(url + "/v1/cluster", {"Authorization": "Basic " + basic})[0], 200)
            self.assertEqual(get(url + "/nope", {"Authorization": "Bearer tok-123"})[0], 404)
            req = urllib.request.Request(url + "/v1/node", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 405)
        finally:
            srv.shutdown()

    def test_cluster_merges_peers(self):
        worker, wurl = serve_app(rackmon.App(FakeNode("spark-2", "worker"), "tok"))
        wrong, xurl = serve_app(rackmon.App(FakeNode("spark-3", "worker"), "different"))
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        os.environ["MONITOR_PEERS"] = "spark-2=%s,spark-3=%s,spark-4=http://127.0.0.1:%d,garbage" % (wurl, xurl, dead_port)
        head, hurl = serve_app(rackmon.App(FakeNode("spark-1"), "tok"))
        try:
            code, c = get(hurl + "/v1/cluster?history=2", {"Authorization": "Bearer tok"})
            self.assertEqual(code, 200)
            names = [n["name"] for n in c["nodes"]]
            self.assertEqual(names, ["spark-1", "spark-2", "spark-3", "spark-4"])
            ok = {n["name"]: n["ok"] for n in c["nodes"]}
            self.assertEqual(ok, {"spark-1": True, "spark-2": True, "spark-3": False, "spark-4": False})
            self.assertIn("token mismatch", c["nodes"][2]["error"])
            self.assertEqual(len(c["nodes"][1]["history"]["t"]), 2)
        finally:
            for s in (worker, wrong, head):
                s.shutdown()
                s.server_close()


class Relay(unittest.TestCase):
    def test_relay_once_over_a_unix_socket(self):
        d = tempfile.mkdtemp()
        sock_path = os.path.join(d, "docker.sock")
        out = os.path.join(d, "containers.json")
        payload = json.dumps([{"Id": "f" * 64, "Names": ["/rack-monitor"], "Image": "rack-monitor:x",
                               "State": "running", "Status": "Up 1 minute", "Created": 5,
                               "Env": ["HF_TOKEN=hf_SECRET"], "Labels": {}}]).encode()
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append((self.command, self.path))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        class UnixServer(ThreadingHTTPServer):
            address_family = socket.AF_UNIX

            def server_bind(self):
                self.socket.bind(self.server_address)
                self.server_name, self.server_port = "localhost", 0

        srv = UnixServer(sock_path, H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rec = rackmon.relay_once(sock_path, out)
            self.assertNotIn("error", rec)
            self.assertEqual(seen, [("GET", "/containers/json?all=1")])
            with open(out) as f:
                text = f.read()
            self.assertEqual(json.loads(text)["containers"][0]["name"], "rack-monitor")
            self.assertNotIn("SECRET", text)
            missing = rackmon.relay_once(os.path.join(d, "absent.sock"), out)
            self.assertIn("error", missing)
        finally:
            srv.shutdown()
            srv.server_close()


class NodeOnThisMachine(unittest.TestCase):
    """The sampler against whatever this machine is. On Linux it must produce
    CPU and memory; elsewhere it must still not raise."""

    def test_sample_twice(self):
        os.environ["MONITOR_STATE_DIR"] = tempfile.mkdtemp()
        os.environ["MONITOR_ENGINE_PORTS"] = "1"          # nothing to discover in a test
        n = rackmon.Node()
        n.sample()
        time.sleep(0.2)
        s = n.sample()
        self.assertEqual(s["schema"], rackmon.SCHEMA)
        self.assertIn("docker relay", s["containers_error"])
        if sys.platform.startswith("linux"):
            self.assertIsNotNone(s["cpu"]["pct"])
            self.assertGreater(s["mem"]["total"], 0)
        snap = n.snapshot(history=5)
        self.assertEqual(len(snap["history"]["t"]), 1)


if __name__ == "__main__":
    unittest.main()
