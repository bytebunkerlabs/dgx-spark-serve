"""rack up --plan: what runs where, for every platform and for 1, 2 and 4
nodes; then rack up and rack down for real against stand-in tools.

    python3 -m unittest discover -s tests
"""
import http.server
import json
import os
import plistlib
import threading
import unittest

from helpers import (FakeMachine, FakeNet, ROOT, dgx_spark, linux_4090x2, mac_m4, wsl_2070, rack_json)

QWEN_REV = "7c41481f57cb95916b40956ab2f0b139b296d974"


def steps(plan, kind=None, node=None):
    return [s for s in plan["steps"] if (kind is None or s["kind"] == kind) and (node is None or s["node"] == node)]


def runs(plan, node=None):
    return [s["argv"] for s in steps(plan, "run", node)]


def docker_run(plan, node=None):
    return [a for a in runs(plan, node) if a[:3] == ["docker", "run", "-d"]]


def after(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A stand-in engine: answers /health once the runtime "starts" it.
ENGINE = ('mkdir -p "$HOME/www" && echo ok > "$HOME/www/health" && '
          '(python3 -m http.server "$STUB_PORT" --bind 127.0.0.1 --directory "$HOME/www" >/dev/null 2>&1 & '
          'echo $! > "$HOME/engine.pid")')


class Base(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.machines = []

    def tearDown(self):
        for m in self.machines:
            pid = os.path.join(m.home, "engine.pid")
            if os.path.exists(pid):
                try:
                    os.kill(int(open(pid).read()), 15)
                except (OSError, ValueError):
                    pass
            m.cleanup()
        self.net.cleanup()

    def machine(self, build, host=None, name="box", ips=(), **kw):
        m = FakeMachine()
        self.machines.append(m)
        build(m, **kw)
        m.hostname(name)
        m.ips(*ips)
        m.logging_cmd("pkill", "exit 1")
        self.net.add(m, *([host] if host else []))
        return m

    def plan(self, m, *args, env=None):
        return rack_json(m.rack("up", *args, "--plan", "--json", extra_env=env))

    def mine(self, m, name, files):
        d = m.config_path("recipes", name)
        os.makedirs(d, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)


TP = lambda n: {"model.env": "MODEL=org/Big\nROLES=chat\n",
                "dgx.env": '. "$RECIPE_DIR/model.env"\nENGINE=vllm\nSERVE_ARGS=(--tensor-parallel-size %d '
                           '--max-model-len 8192)\n' % n,
                "linux.env": '. "$RECIPE_DIR/model.env"\nENGINE=vllm\nSERVE_ARGS=(--tensor-parallel-size %d)\n' % n}


class Plans(Base):
    def test_mac_llamacpp_under_launchd(self):
        m = self.machine(mac_m4, name="mini")
        p = self.plan(m, "qwen3-8b")
        self.assertEqual((p["platform"], p["engine"], p["runtime"], p["host"], p["port"], p["memory_cap_gb"]),
                         ("mac", "llamacpp", "launchd", "0.0.0.0", 8888, None))
        self.assertEqual([s["kind"] for s in p["steps"]],
                         ["free", "install", "weights", "run", "write", "run", "run", "wait", "record"])
        inst = steps(p, "install")[0]
        self.assertEqual(inst["assets"][0]["url"],
                         "https://github.com/ggml-org/llama.cpp/releases/download/b11430/llama-b11430-bin-macos-arm64.tar.gz")
        job = plistlib.loads(steps(p, "write")[0]["content"].encode())
        args = job["ProgramArguments"]
        self.assertTrue(args[0].endswith("/engines/llama.cpp/b11430-macos-arm64/llama-b11430/llama-server"))
        gguf = after(args, "-m")[0]
        self.assertTrue(gguf.endswith("/hub/models--Qwen--Qwen3-8B-GGUF/snapshots/%s/Qwen3-8B-Q4_K_M.gguf" % QWEN_REV))
        self.assertEqual((after(args, "--alias"), after(args, "--port"), after(args, "--ctx-size")),
                         (["qwen3-8b"], ["8888"], ["16384"]))
        self.assertEqual(after(args, "--api-key-file"), [m.config_path("engine.key")])
        self.assertEqual((job["Label"], job["KeepAlive"], job["RunAtLoad"]),
                         ("ai.bytebunker.dgx-serve.engine", {"SuccessfulExit": False}, True))
        self.assertEqual(p["serving"]["dialect"]["thinking"], "chat_template_kwargs.enable_thinking")

    def test_dgx_solo_in_docker(self):
        m = self.machine(dgx_spark)
        p = self.plan(m, "phase1")
        (argv,) = docker_run(p)
        self.assertEqual(argv[:8], ["docker", "run", "-d", "--name", "serve_solo", "--restart", "unless-stopped", "--label"])
        for flag in ("--memory=112g", "--memory-swap=112g", "--entrypoint=", "--ipc=host"):
            self.assertIn(flag, argv)
        self.assertEqual(after(argv, "--log-driver"), ["journald"])
        self.assertEqual(after(argv, "--env-file"), [m.config_path("engine.env")])
        self.assertIn("HF_HUB_OFFLINE=1", after(argv, "-e"))
        i = argv.index("--entrypoint=")
        self.assertEqual(argv[i + 1:i + 5], ["dgx-spark-serve:dev", "vllm", "serve", "Qwen/Qwen3-8B"])
        self.assertEqual(argv[-6:], ["--host", "0.0.0.0", "--port", "8888", "--served-model-name", "phase1-qwen3-8b"])
        whats = [s["what"] for s in p["steps"]]
        self.assertIn("drop the page cache before a big load", whats)
        self.assertTrue(any(s["kind"] == "spawn" and "memwatch.sh" in " ".join(s["argv"]) for s in p["steps"]))
        self.assertEqual(steps(p, "wait")[0]["url"], "http://127.0.0.1:8888/health")

    def test_linux_uses_the_recipes_image_and_no_spark_extras(self):
        m = self.machine(linux_4090x2)
        p = self.plan(m, "qwen3-8b")
        (argv,) = docker_run(p)
        self.assertIn("vllm/vllm-openai:v0.26.0", argv)
        self.assertIn("--memory=117g", argv)            # 125 GiB - 8
        self.assertFalse([s for s in p["steps"] if "drop" in s["what"] or s["kind"] == "spawn"])
        # two GPUs: tensor parallel 2 stays inside this box
        self.mine(m, "big", TP(2))
        self.assertEqual(len(self.plan(m, "big")["nodes"]), 1)
        self.mine(m, "huge", TP(4))
        r = m.rack("up", "huge", "--plan")
        self.assertIn("tensor parallel 4 needs 4 GPUs in one machine; this one has 2", r.stderr)

    def test_windows_llamacpp_under_systemd(self):
        m = self.machine(wsl_2070, name="pc")
        p = self.plan(m, "qwen3-8b")
        self.assertEqual((p["runtime"], p["engine"], p["memory_cap_gb"]), ("systemd", "llamacpp", 11))
        names = [a["url"].rsplit("/", 1)[-1] for a in steps(p, "install")[0]["assets"]]
        self.assertEqual(names, ["llama-b11430-bin-ubuntu-cuda-12.8-x64.tar.gz",
                                 "cudart-llama-b11430-bin-ubuntu-cuda-12.8-x64.tar.gz"])
        unit = [s for s in steps(p, "write") if s["path"].endswith("dgx-serve-engine.service")][0]["content"]
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("MemoryMax=11G", unit)
        self.assertRegex(unit, r"Environment=LD_LIBRARY_PATH=\S+/llama-b11430:\S+/cudart-llama-b11430-bin-ubuntu-cuda-12.8-x64")
        self.assertRegex(unit, r"ExecStart=\S+/llama-server -m \S+Qwen3-8B-Q4_K_M.gguf --host 0.0.0.0 --port 8888 "
                               r"--alias qwen3-8b --api-key-file \S+engine.key --n-gpu-layers 999 --ctx-size 8192")
        self.assertEqual(runs(p)[-3:], [["systemctl", "--user", "daemon-reload"],
                                        ["systemctl", "--user", "enable", "dgx-serve-engine.service"],
                                        ["systemctl", "--user", "restart", "dgx-serve-engine.service"]])

    def test_an_old_driver_is_refused_with_the_fix(self):
        m = self.machine(wsl_2070)
        m.cmd("nvidia-smi", 'case "$*" in *--query-gpu*) echo "NVIDIA GeForce RTX 2070, 8192, 7.5, 551.86";; '
                            '*) echo "CUDA Version: 12.4";; esac')
        r = m.rack("up", "qwen3-8b", "--plan")
        self.assertIn("CUDA 12.8 or newer (this one is 12.4): update the NVIDIA driver on Windows", r.stderr)

    def test_two_sparks(self):
        m = self.machine(dgx_spark, name="burhan", ips=[("enp1s0f0np0", "192.168.100.1")])
        self.machine(dgx_spark, host="spark-2", name="aleem", ips=[("enp1s0f0np0", "192.168.100.2")])
        p = self.plan(m, "phase2-gpt-oss-120b")
        self.assertEqual([n["name"] for n in p["nodes"]], ["spark-1", "spark-2"])
        self.assertEqual(p["master"], "192.168.100.1:29501")
        head, worker = docker_run(p, "spark-1")[0], docker_run(p, "spark-2")[0]
        for argv, ip in ((head, "192.168.100.1"), (worker, "192.168.100.2")):
            env = after(argv, "-e")
            self.assertIn("VLLM_HOST_IP=" + ip, env)
            self.assertIn("NCCL_SOCKET_IFNAME=enp1s0f0np0", env)
            self.assertIn("NCCL_IB_HCA=rocep1s0f0,roceP2p1s0f0", env)
            self.assertEqual(argv[-2:], ["sleep", "infinity"])
            self.assertIn("--privileged", argv)
        self.assertEqual(after(head, "--env-file"), [m.config_path("engine.env")])
        self.assertEqual(after(worker, "--env-file"), [])                      # headless: no API, no key
        self.assertIn("~/.cache/vllm:/root/.cache/vllm", after(worker, "-v"))    # the worker's own home
        scripts = {s["node"]: s["content"] for s in steps(p, "write") if s.get("container") == "serve_node"}
        self.assertIn("--nnodes 2 --node-rank 1 --master-addr 192.168.100.1 --master-port 29501 --headless",
                      scripts["spark-2"])
        self.assertIn("--node-rank 0 --master-addr 192.168.100.1 --master-port 29501 --host 0.0.0.0 --port 8888",
                      scripts["spark-1"])
        launches = [s["node"] for s in steps(p, "run") if s["argv"][:3] == ["docker", "exec", "-d"]]
        self.assertEqual(launches, ["spark-2", "spark-1"])                      # workers first
        boot = [s for s in steps(p, "write") if s["path"].endswith("dgx-serve-boot.service")][0]["content"]
        self.assertIn("ExecStart=%s/rack up phase2-gpt-oss-120b --dgx --boot" % ROOT, boot)
        # a recipe from before 1.0 that sets --host/--port is honoured, said so, and not doubled
        self.assertTrue(any("--host 0.0.0.0 itself" in n for n in p["notes"]))
        self.assertEqual(scripts["spark-1"].count("--port"), 1)

    def four_sparks(self):
        m = self.machine(dgx_spark, name="s1", ips=[("f0", "10.9.0.1")])
        self.assertEqual(m.rack("init", "--name", "s1").returncode, 0)
        for i in (2, 3, 4):
            self.assertEqual(m.rack("nodes", "add", "s%d" % i, "--dgx", "--no-probe", "--fabric", "10.9.0.%d" % i).returncode, 0)
        return m

    def test_four_sparks(self):
        m = self.four_sparks()
        self.mine(m, "big", TP(4))
        p = self.plan(m, "big")
        self.assertEqual([n["name"] for n in p["nodes"]], ["s1", "s2", "s3", "s4"])
        self.assertEqual(p["master"], "10.9.0.1:29501")
        scripts = {s["node"]: s["content"] for s in steps(p, "write") if s.get("container") == "serve_node"}
        for rank, n in enumerate(["s1", "s2", "s3", "s4"]):
            self.assertIn("--nnodes 4 --node-rank %d " % rank, scripts[n])
        self.assertEqual([s["node"] for s in steps(p, "run") if s["argv"][:3] == ["docker", "exec", "-d"]],
                         ["s4", "s3", "s2", "s1"])
        self.assertIn("NCCL_SOCKET_IFNAME=f0", after(docker_run(p, "s3")[0], "-e"))
        # two of four: the first workers by name
        self.mine(m, "pair", TP(2))
        self.assertEqual([n["name"] for n in self.plan(m, "pair")["nodes"]], ["s1", "s2"])

    def test_not_enough_machines(self):
        m = self.four_sparks()
        self.mine(m, "giant", TP(8))
        self.assertIn("spans 8 machines and the rack has 4", m.rack("up", "giant", "--plan").stderr)
        self.assertEqual(len(self.plan(m, "giant", env={"TOPOLOGY": "solo"})["nodes"]), 1)

    def test_without_a_key_the_engine_stays_local(self):
        m = self.machine(dgx_spark)
        p = self.plan(m, "phase1", env={"ENGINE_KEY": "off"})
        (argv,) = docker_run(p)
        self.assertEqual((p["host"], p["key_required"], after(argv, "--env-file")), ("127.0.0.1", False, []))
        self.assertTrue(any("ENGINE_KEY=off" in n for n in p["notes"]))

    def test_planning_for_another_platform(self):
        m = self.machine(dgx_spark)
        p = self.plan(m, "qwen3-8b", "--mac")
        self.assertEqual((p["runtime"], p["assumed_facts"]), ("launchd", True))
        human = m.rack("up", "qwen3-8b", "--mac", "--plan").stdout
        self.assertIn("planned for a typical mac, not this machine", human)


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *a):
        pass


class Execute(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def env(self, **kw):
        port = str(free_port())
        e = {"API_PORT": port, "STUB_PORT": port, "RACK_POLL": "0.05"}
        e.update(kw)
        return e

    def weights(self, m, repo="Qwen/Qwen3-8B"):
        snap = os.path.join(m.home, "hf", "hub", "models--" + repo.replace("/", "--"), "snapshots", "abc")
        os.makedirs(snap)
        with open(os.path.join(snap, "model.safetensors.index.json"), "w") as f:
            json.dump({"weight_map": {"a": "model-1.safetensors"}}, f)
        open(os.path.join(snap, "model-1.safetensors"), "w").close()
        m.dotenv("HF_CACHE=%s/hf\n" % m.home)

    def test_dgx_solo_up_and_down(self):
        m = self.machine(dgx_spark)
        self.weights(m)
        m.logging_cmd("docker", 'case "$1" in inspect) echo true;; run) %s; echo 0123abcd;; esac' % ENGINE)
        m.logging_cmd("sudo", "exit 1")
        env = self.env()
        r = m.rack("up", "phase1", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("== healthy", r.stdout)
        calls = m.log("docker")
        self.assertTrue(any(c.startswith("run -d --name serve_solo --restart unless-stopped") for c in calls), calls)
        self.assertIn("WARN: could not drop caches", r.stderr)
        rec = json.load(open(os.path.join(m.home, ".local", "state", "dgx-serve", "serving.json")))
        self.assertEqual((rec["recipe"], rec["runtime"], rec["port"], rec["nodes"]),
                         ("phase1-qwen3-8b", "docker", int(env["API_PORT"]), ["box"]))
        self.assertTrue(rec["started_at"])
        key = open(m.config_path("engine.key")).read().strip()
        self.assertNotIn(key, r.stdout + r.stderr)                    # made on first use, never shown
        # and down undoes it
        r = m.rack("down", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rm -f serve_solo serve_node", m.log("docker"))
        self.assertFalse(os.path.exists(os.path.join(m.home, ".local", "state", "dgx-serve", "serving.json")))

    def test_refuses_to_start_on_top_of_another_engine(self):
        m = self.machine(dgx_spark)
        self.weights(m)
        m.logging_cmd("docker", 'case "$1" in ps) echo serve_node;; inspect) echo true;; esac')
        r = m.rack("up", "phase1", extra_env=self.env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("box is already serving (serve_node): rack down first, or rack up --replace", r.stderr)
        self.assertFalse([c for c in m.log("docker") if c.startswith("run")])
        m.dotenv("HF_CACHE=%s/hf\nFOREIGN_ENGINES=vllm-fn\n" % m.home)
        m.logging_cmd("docker", 'case "$1" in ps) echo vllm-fn;; esac')
        r = m.rack("up", "phase1", "--replace", extra_env=self.env())
        self.assertIn("already serving (vllm-fn)", r.stderr)              # --replace never touches another stack

    def test_something_else_on_the_port(self):
        m = self.machine(dgx_spark)
        self.weights(m)
        m.logging_cmd("docker", "exit 0")
        r = m.rack("up", "phase1", extra_env=self.env(API_PORT=str(self.port)))
        self.assertIn("something on port %d" % self.port, r.stderr)

    def test_mac_up_and_down(self):
        m = self.machine(mac_m4, name="mini")
        state = os.path.join(m.home, ".local", "state", "dgx-serve")
        server = os.path.join(state, "engines", "llama.cpp", "b11430-macos-arm64", "llama-b11430", "llama-server")
        os.makedirs(os.path.dirname(server))
        open(server, "w").close()                                       # installed already: no download
        repo = os.path.join(m.home, "dgx", "hf", "hub", "models--Qwen--Qwen3-8B-GGUF")
        os.makedirs(os.path.join(repo, "snapshots", QWEN_REV))
        open(os.path.join(repo, "snapshots", QWEN_REV, "Qwen3-8B-Q4_K_M.gguf"), "w").close()
        # an engine already running under launchd blocks a second one
        m.logging_cmd("launchctl", 'case "$1" in print) echo "state = running";; esac')
        r = m.rack("up", "qwen3-8b", extra_env=self.env())
        self.assertIn("mini is already serving (launchd job ai.bytebunker.dgx-serve.engine)", r.stderr)
        m.logging_cmd("launchctl", 'case "$1" in print) [ -f "$HOME/loaded" ] && echo "state = running" || exit 113;; '
                                   'bootstrap) touch "$HOME/loaded"; %s;; bootout) rm -f "$HOME/loaded";; esac' % ENGINE)
        env = self.env()
        r = m.rack("up", "qwen3-8b", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plist = os.path.join(m.home, "Library", "LaunchAgents", "ai.bytebunker.dgx-serve.engine.plist")
        job = plistlib.load(open(plist, "rb"))
        self.assertEqual(job["ProgramArguments"][0], server)
        self.assertIn("bootstrap gui/%d %s" % (os.getuid(), plist), m.log("launchctl"))
        r = m.rack("down", extra_env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(plist))
        self.assertIn("bootout gui/%d/ai.bytebunker.dgx-serve.engine" % os.getuid(), m.log("launchctl"))

    def test_missing_weights_are_pulled_first(self):
        m = self.machine(dgx_spark)
        m.dotenv("HF_CACHE=%s/hf\n" % m.home)
        m.logging_cmd("docker", 'case "$1" in image) exit 0;; esac')
        r = m.rack("up", "phase1", extra_env=self.env())
        self.assertIn("not here yet: rack pull phase1-qwen3-8b --dgx", r.stdout)
        self.assertIn("cannot reach http://127.0.0.1:9", r.stderr)      # tests never reach the real hub
        self.assertIn("rack pull phase1-qwen3-8b failed", r.stderr)
        self.assertFalse([c for c in m.log("docker") if c.startswith("run")])


class Down(Base):
    def test_down_without_a_record_stops_every_node(self):
        m = self.machine(dgx_spark, name="burhan", ips=[("enp1s0f0np0", "192.168.100.1")])
        w = self.machine(dgx_spark, host="spark-2", name="aleem")
        m.logging_cmd("docker")
        w.logging_cmd("docker")
        m.dotenv("FOREIGN_ENGINES=vllm-fn\n")
        r = m.rack("down")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rm -f serve_solo serve_node vllm-fn", m.log("docker"))
        self.assertIn("rm -f serve_solo serve_node vllm-fn", w.log("docker"))
        p = rack_json(m.rack("down", "--plan", "--json"))
        self.assertEqual(p["command"], "down")


if __name__ == "__main__":
    unittest.main()
