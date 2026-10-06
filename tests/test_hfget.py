"""py/hfget.py and rack pull, against a fake Hugging Face hub: the listing,
the cache layout, Range resume, hash checks, gated repos, the token never
reaching the CDN, the disk check.

    python3 -m unittest discover -s tests
"""
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse

from helpers import FakeMachine, ROOT, mac_m4, dgx_spark, rack_json

sys.path.insert(0, os.path.join(ROOT, "py"))
import fetch  # noqa: E402
import hfget  # noqa: E402

COMMIT = "c0ffee" + "0" * 34


def git_sha1(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Hub:
    """Two servers: the hub (API, small files, redirects) and its CDN (LFS files)."""

    def __init__(self):
        self.repos, self.seen, self.corrupt, self.token = {}, [], set(), "tok123"
        hub, cdn = self, self

        class CDN(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hub.seen.append(("cdn", self.path, dict(self.headers)))
                key = urllib.parse.unquote(self.path.lstrip("/"))
                data = hub.blobs.get(key)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if key in hub.corrupt:
                    data = b"x" * len(data)
                send(self, data)

            def log_message(self, *a):
                pass

        class API(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hub.seen.append(("hub", self.path, dict(self.headers)))
                u = urllib.parse.urlparse(self.path)
                parts = u.path.strip("/").split("/")
                if parts[:2] == ["api", "models"]:
                    repo = "/".join(parts[2:4])
                    r = hub.repos.get(repo)
                    if r is None:
                        return self.fail(404)
                    if r.get("gated") and self.headers.get("Authorization") != "Bearer " + hub.token:
                        return self.fail(401)
                    sib = []
                    for path, spec in sorted(r["files"].items()):
                        data, lfs = spec[0], spec[1]
                        size = spec[2] if len(spec) > 2 else len(data)      # a size to report, not serve
                        s = {"rfilename": path, "size": size, "blobId": git_sha1(data)}
                        if lfs:
                            s["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": size}
                        sib.append(s)
                    body = json.dumps(dict({"id": repo, "sha": COMMIT, "siblings": sib}, **r.get("meta", {}))).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if len(parts) >= 5 and parts[2] == "resolve":
                    repo, path = "/".join(parts[:2]), urllib.parse.unquote("/".join(parts[4:]))
                    r = hub.repos.get(repo)
                    if r is None or path not in r["files"]:
                        return self.fail(404)
                    if r.get("gated") and self.headers.get("Authorization") != "Bearer " + hub.token:
                        return self.fail(401)
                    data, lfs = r["files"][path][:2]
                    if lfs:                                   # LFS files live on the CDN
                        key = hashlib.sha256(data).hexdigest()
                        hub.blobs[key] = data
                        self.send_response(302)
                        self.send_header("Location", "http://localhost:%d/%s" % (hub.cdn_port, key))
                        self.end_headers()
                        return
                    return send(self, data)
                self.fail(404)

            def fail(self, code):
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        def send(h, data):
            start = 0
            rng = h.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
                h.send_response(206)
                h.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            else:
                h.send_response(200)
            h.send_header("Content-Length", str(len(data) - start))
            h.end_headers()
            h.wfile.write(data[start:])

        self.blobs = {}
        self.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), API)
        self.cdn = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CDN)
        self.cdn_port = self.cdn.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.api.server_address[1]
        for s in (self.api, self.cdn):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def close(self):
        for s in (self.api, self.cdn):
            s.shutdown()
            s.server_close()


WEIGHTS = os.urandom(300000)
GGUF = os.urandom(120000)


def model_repo(gated=False):
    return {"gated": gated, "files": {
        "config.json": (b'{"model_type": "qwen3"}', False),
        "tokenizer.json": (b'{"version": "1.0"}', False),
        "model-00001-of-00001.safetensors": (WEIGHTS, True),
        "model.safetensors.index.json": (b'{"weight_map": {"w": "model-00001-of-00001.safetensors"}}', False),
        "original/consolidated.00.pth": (b"o" * 5000, True),
        "model-Q4_K_M.gguf": (GGUF, True),
        ".gitattributes": (b"*.safetensors filter=lfs", False),
        "copy/config.json": (b'{"model_type": "qwen3"}', False),       # the same blob as config.json
    }}


class Base(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.hub.repos["org/model"] = model_repo()
        self.tmp = tempfile.mkdtemp(prefix="hfget-")
        self.cache = os.path.join(self.tmp, "hf")
        self.env = {"HF_ENDPOINT": self.hub.url, "DGX_SERVE_CONFIG": os.path.join(self.tmp, "cfg"),
                    "HF_TOKEN": "", "HUGGING_FACE_HUB_TOKEN": "", "HOME": self.tmp}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.hub.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rd(self, repo="org/model"):
        return os.path.join(self.cache, "hub", "models--" + repo.replace("/", "--"))


class Download(Base):
    def test_weights_land_in_the_hub_layout(self):
        s = hfget.pull("org/model", weights=True, cache=self.cache, quiet=True)
        self.assertEqual(sorted(s["files"]), ["config.json", "copy/config.json", "model-00001-of-00001.safetensors",
                                               "model.safetensors.index.json", "tokenizer.json"])
        rd = self.rd()
        snap = os.path.join(rd, "snapshots", COMMIT)
        self.assertEqual(open(os.path.join(rd, "refs", "main")).read(), COMMIT)
        w = os.path.join(snap, "model-00001-of-00001.safetensors")
        self.assertEqual(os.readlink(w), "../../blobs/" + hashlib.sha256(WEIGHTS).hexdigest())
        self.assertEqual(open(w, "rb").read(), WEIGHTS)
        self.assertEqual(os.readlink(os.path.join(snap, "copy", "config.json")),
                         "../../../blobs/" + git_sha1(b'{"model_type": "qwen3"}'))
        self.assertFalse(os.path.exists(os.path.join(snap, "model-Q4_K_M.gguf")))
        self.assertFalse(os.path.exists(os.path.join(snap, "original")))
        gets = [p for kind, p, _ in self.hub.seen if "config.json" in p and kind == "hub"]
        self.assertEqual(len(gets), 1)                                 # one blob, two paths
        # a second pull fetches nothing
        n = len(self.hub.seen)
        hfget.pull("org/model", weights=True, cache=self.cache, quiet=True)
        self.assertEqual(len(self.hub.seen), n + 1)                    # the listing only

    def test_resume_with_a_range_request(self):
        blob = os.path.join(self.rd(), "blobs", hashlib.sha256(WEIGHTS).hexdigest())
        os.makedirs(os.path.dirname(blob))
        with open(blob + ".part", "wb") as f:
            f.write(WEIGHTS[:100000])
        hfget.pull("org/model", files=["model-00001-of-00001.safetensors"], cache=self.cache, quiet=True)
        ranges = [h.get("Range") for kind, _, h in self.hub.seen if kind == "cdn"]
        self.assertEqual(ranges, ["bytes=100000-"])
        self.assertEqual(open(blob, "rb").read(), WEIGHTS)

    def test_a_corrupt_file_is_refused_and_removed(self):
        self.hub.corrupt.add(hashlib.sha256(GGUF).hexdigest())
        with self.assertRaises(hfget.HubError) as e:
            hfget.pull("org/model", files=["model-Q4_K_M.gguf"], cache=self.cache, quiet=True)
        self.assertIn("sha256", str(e.exception))
        self.assertEqual(os.listdir(os.path.join(self.rd(), "blobs")), [])

    def test_a_gated_repo_needs_a_token_and_the_cdn_never_sees_it(self):
        self.hub.repos["org/gated"] = model_repo(gated=True)
        with self.assertRaises(hfget.HubError) as e:
            hfget.pull("org/gated", weights=True, cache=self.cache, quiet=True)
        self.assertIn("org/gated is gated or private", str(e.exception))
        self.assertIn("~/.config/dgx-serve/hf-token", str(e.exception))
        os.makedirs(os.environ["DGX_SERVE_CONFIG"])
        with open(os.path.join(os.environ["DGX_SERVE_CONFIG"], "hf-token"), "w") as f:
            f.write("tok123\n")
        hfget.pull("org/gated", weights=True, cache=self.cache, quiet=True)
        cdn = [h for kind, _, h in self.hub.seen if kind == "cdn"]
        self.assertTrue(cdn)
        self.assertFalse([h for h in cdn if "Authorization" in h])
        self.assertTrue([h for kind, _, h in self.hub.seen if kind == "hub" and h.get("Authorization") == "Bearer tok123"])

    def test_one_gguf_file_at_a_pinned_commit(self):
        s = hfget.pull("org/model", revision=COMMIT, files=["model-Q4_K_M.gguf"], cache=self.cache, quiet=True)
        self.assertEqual(s["files"], ["model-Q4_K_M.gguf"])
        self.assertEqual(open(os.path.join(self.rd(), "snapshots", COMMIT, "model-Q4_K_M.gguf"), "rb").read(), GGUF)
        self.assertFalse(os.path.exists(os.path.join(self.rd(), "refs")) and os.listdir(os.path.join(self.rd(), "refs")))

    def test_disk_check_and_dry_run(self):
        s = hfget.pull("org/model", weights=True, cache=self.cache, dry_run=True, quiet=True)
        self.assertEqual((s["to_download"], s["commit"]), (s["bytes"], COMMIT))
        self.assertFalse(os.path.exists(self.rd()))
        with self.assertRaises(hfget.HubError) as e:
            hfget.pull("org/model", weights=True, cache=self.cache, reserve_gb=1e12, quiet=True)
        self.assertIn("make room, or point HF_CACHE at a bigger disk", str(e.exception))

    def test_unknown_repo_and_file(self):
        with self.assertRaises(hfget.HubError) as e:
            hfget.pull("org/nope", cache=self.cache, quiet=True)
        self.assertIn("org/nope@main is not on the hub", str(e.exception))
        with self.assertRaises(hfget.HubError) as e:
            hfget.pull("org/model", files=["nope.gguf"], cache=self.cache, quiet=True)
        self.assertIn("not in the repo: nope.gguf", str(e.exception))


QWEN3_8B = {"num_hidden_layers": 36, "num_attention_heads": 32, "num_key_value_heads": 8, "head_dim": 128,
            "hidden_size": 4096, "max_position_embeddings": 40960}
GB = 10 ** 9


class Fit(Base):
    """rack fit, on the hub's numbers (reported sizes; nothing is downloaded)."""

    def setUp(self):
        super().setUp()
        cfg = json.dumps(QWEN3_8B).encode()
        self.hub.repos["org/big"] = {"meta": {"safetensors": {"total": 8190735360}}, "files": {
            "config.json": (cfg, False),
            "model-00001-of-00002.safetensors": (b"a", True, 8 * GB),
            "model-00002-of-00002.safetensors": (b"b", True, int(8.4 * GB)),
            "model.safetensors.index.json": (b"{}", False)}}
        self.hub.repos["org/big-GGUF"] = {"meta": {"gguf": {"total": 8190735360, "context_length": 40960},
                                                   "cardData": {"base_model": "org/big"}}, "files": {
            "big-Q4_K_M.gguf": (b"q4", True, int(5.0 * GB)),
            "big-Q8_0.gguf": (b"q8", True, int(8.7 * GB)),
            "big-F16.gguf": (b"f16", True, int(16.4 * GB))}}
        import fit
        self.fit = fit

    def test_kv_cache_per_token(self):
        self.assertEqual(self.fit.kv_bytes_per_token(QWEN3_8B), 147456)          # 144 KiB, bf16
        mla = {"num_hidden_layers": 61, "kv_lora_rank": 512, "qk_rope_head_dim": 64}
        self.assertEqual(self.fit.kv_bytes_per_token(mla), 61 * 576 * 2)
        self.assertIsNone(self.fit.kv_bytes_per_token({}))

    def test_one_spark_and_two(self):
        r = self.fit.fit("dgx", 124610, 2, repo="org/big")
        self.assertGreaterEqual(r["download_bytes"], int(16.4 * GB))
        self.assertEqual((r["parameters"], r["native_context"]), (8190735360, 40960))
        self.assertEqual([(v["nodes"], v["fits"], v["max_context"]) for v in r["verdicts"]],
                         [(1, True, 40960), (2, True, 40960)])

    def test_a_small_gpu_reads_the_gguf_quants(self):
        r = self.fit.fit("windows", 8192, 1, repo="org/big-GGUF")        # an 8 GB RTX 2070
        self.assertEqual([(q["file"], q["fits"]) for q in r["quants"]],
                         [("big-Q4_K_M.gguf", True), ("big-Q8_0.gguf", False), ("big-F16.gguf", False)])
        self.assertEqual(r["quants"][0]["max_context"], 11528)          # 1.7 GB of room at 144 KiB a token
        spec = {"pulls": [{"repo": "org/big-GGUF", "revision": "main", "files": ["big-Q4_K_M.gguf"]}],
                "model": "org/big", "context": 8192, "recipe": "big"}
        r = self.fit.fit("windows", 8192, 1, spec=spec)                   # the recipe's 8k window fits
        self.assertEqual((r["context"], r["verdicts"][0]["fits"]), (8192, True))

    def test_rack_fit_on_a_mac(self):
        m = FakeMachine()
        try:
            mac_m4(m)                                                     # 16 GB: about 10.7 GB for the GPU
            r = m.rack("fit", "org/big-GGUF", "--json", extra_env={"HF_ENDPOINT": self.hub.url})
            d = rack_json(r)
            self.assertEqual((d["platform"], d["budget_mb"]), ("mac", 10922))
            self.assertEqual([q["fits"] for q in d["quants"]], [True, True, False])
            human = m.rack("fit", "org/big-GGUF", extra_env={"HF_ENDPOINT": self.hub.url}).stdout
            self.assertIn("about 11 GiB of the Metal working-set limit on this Mac", human)
            self.assertNotIn("dgx", m.rack("fit", "org/big", "--dgx", extra_env={"HF_ENDPOINT": self.hub.url}).stdout)
        finally:
            m.cleanup()


class RackPull(Base):
    def setUp(self):
        super().setUp()
        self.m = FakeMachine()

    def tearDown(self):
        self.m.cleanup()
        super().tearDown()

    def recipe(self, files):
        d = self.m.config_path("recipes", "tiny")
        os.makedirs(d)
        for fn, text in files.items():
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)

    def test_mac_variant_pulls_its_one_gguf_file(self):
        mac_m4(self.m)
        self.recipe({"model.env": "MODEL=org/model\n",
                     "mac.env": '. "$RECIPE_DIR/model.env"\nENGINE=llamacpp\nARTIFACT=org/model/model-Q4_K_M.gguf\n'
                                'SERVE_ARGS=(--ctx-size 4096)\n'})
        env = {"HF_ENDPOINT": self.hub.url, "HF_CACHE": os.path.join(self.m.home, "hf")}
        plan = rack_json(self.m.rack("pull", "tiny", "--plan", "--json", extra_env=env))
        self.assertEqual((plan["engine"], plan["pulls"][0]["files"]), ("llamacpp", ["model-Q4_K_M.gguf"]))
        r = self.m.rack("pull", "tiny", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = os.path.join(self.m.home, "hf", "hub", "models--org--model", "snapshots", COMMIT, "model-Q4_K_M.gguf")
        self.assertEqual(open(f, "rb").read(), GGUF)
        # rack up finds it through refs/main
        p = rack_json(self.m.rack("up", "tiny", "--plan", "--json", extra_env=env))
        self.assertIn("/snapshots/{revision}/model-Q4_K_M.gguf", json.dumps(p))

    def test_a_repo_id_pulls_vllms_weights_and_verifies(self):
        dgx_spark(self.m)
        self.m.hostname("box")
        env = {"HF_ENDPOINT": self.hub.url, "HF_CACHE": os.path.join(self.m.home, "hf"), "WORKER_SSH": ""}
        r = self.m.rack("pull", "org/model", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("box: 1 shards, 0 missing", r.stdout.replace("\x1b[0m", ""))


if __name__ == "__main__":
    unittest.main()
