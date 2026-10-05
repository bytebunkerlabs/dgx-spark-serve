"""The inventory: rack init, rack nodes, and how every command finds the head
and its workers, on one machine or many, with or without a .env.

    python3 -m unittest discover -s tests
"""
import json
import os
import unittest

from helpers import (FakeMachine, FakeNet, dgx_spark, linux_4090x2, linux_no_gpu, mac_m4,
                     rack_json, ROOT)

SITE = 'load_site_env; rack_resolve_site; echo "$IS_HEAD|$WORKER_NAMES|$WORKERS|$HEAD_LABEL|${HEAD_IP:-}|${WORKER_IP:-}|${HEAD_SSH:-}"'


def node_file(m, name):
    p = m.config_path("nodes", name + ".env")
    return dict(l.split("=", 1) for l in open(p).read().splitlines()) if os.path.exists(p) else None


class Base(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.machines = []

    def tearDown(self):
        for m in self.machines:
            m.cleanup()
        self.net.cleanup()

    def machine(self, build=None, host=None, name="box", ips=(), **kw):
        m = FakeMachine()
        self.machines.append(m)
        if build:
            build(m, **kw)
        m.hostname(name)
        m.ips(*ips)
        self.net.add(m, *([host] if host else []))
        for tool in ("docker", "rsync", "ping", "journalctl", "loginctl", "free", "curl"):
            m.log_calls(tool)
        return m

    def site(self, m, extra_env=None):
        r = m.bash(SITE, extra_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().split("|")

    def ok(self, proc):
        self.assertEqual(proc.returncode, 0, "rc=%d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc


class Resolution(Base):
    """Who is the head and who are the workers, before and after rack init."""

    def test_single_box_without_config_is_its_own_head_with_no_worker(self):
        m = self.machine(linux_4090x2, name="gpu-box")
        self.assertEqual(self.site(m)[:4], ["1", "", "", "gpu-box"])

    def test_empty_worker_ssh_stays_empty_even_on_the_authors_rack(self):
        m = self.machine(dgx_spark, ips=[("enp1s0f0np0", "192.168.100.1")])
        self.assertEqual(self.site(m, {"WORKER_SSH": ""})[:4], ["1", "", "", "spark-1"])

    def test_authors_rack_without_env_keeps_spark_1_and_spark_2(self):
        m = self.machine(dgx_spark, name="burhan", ips=[("enp1s0f0np0", "192.168.100.1")])
        self.assertEqual(self.site(m)[:6], ["1", "spark-2", "spark-2", "spark-1", "192.168.100.1", "192.168.100.2"])

    def test_authors_worker_drives_spark_1(self):
        m = self.machine(dgx_spark, name="aleem", ips=[("enp1s0f0np0", "192.168.100.2")])
        s = self.site(m)
        self.assertEqual((s[0], s[3], s[6]), ("0", "spark-1", "spark-1"))

    def test_a_non_spark_on_the_same_subnet_gets_no_phantom_worker(self):
        m = self.machine(linux_4090x2, ips=[("eth0", "192.168.100.1")])
        self.assertEqual(self.site(m)[:3], ["1", "", ""])

    def test_cloud_example_head_ssh_localhost_is_the_head(self):
        m = self.machine(linux_4090x2, name="cloud1")
        m.dotenv("HEAD_SSH=localhost\nWORKER_SSH=\nTOPOLOGY=solo\n")
        self.assertEqual(self.site(m)[:4], ["1", "", "", "cloud1"])

    def test_head_ssh_naming_another_machine_means_a_laptop(self):
        m = self.machine(mac_m4)
        s = self.site(m, {"HEAD_SSH": "gpu1"})
        self.assertEqual((s[0], s[3], s[6]), ("0", "gpu1", "gpu1"))

    def test_head_detection_needs_no_fabric(self):
        m = self.machine(linux_4090x2, ips=[("eth0", "10.0.0.7")])
        m.dotenv("WORKER_SSH=gpu2\nWORKER_IP=10.0.0.8\n")
        self.assertEqual(self.site(m)[:3], ["1", "gpu2", "gpu2"])

    def write_inventory(self, m, nodes):
        for name, kv in nodes.items():
            r = m.bash("inv_set %s %s" % (name, " ".join("%s=%s" % x for x in kv.items())))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_inventory_head_with_two_workers(self):
        m = self.machine(dgx_spark)
        self.write_inventory(m, {
            "s1": {"NODE_LOCAL": 1, "NODE_ROLE": "head", "NODE_FABRIC_IP": "10.9.0.1", "NODE_FABRIC_IF": "f0"},
            "s3": {"NODE_ROLE": "worker", "NODE_SSH": "admin@s3.lan", "NODE_FABRIC_IP": "10.9.0.3"},
            "s2": {"NODE_ROLE": "worker", "NODE_SSH": "s2", "NODE_FABRIC_IP": "10.9.0.2"},
            "mini": {"NODE_ROLE": "node", "NODE_SSH": "mini", "NODE_PLATFORM": "mac"},
        })
        self.assertEqual(self.site(m)[:6], ["1", "s2 s3", "s2 admin@s3.lan", "s1", "10.9.0.1", "10.9.0.2"])
        r = m.bash('rack_resolve_site; echo "$FABRIC_IF"; inv_names | tr "\\n" " "')
        self.assertEqual(r.stdout.split("\n")[:2], ["f0", "s1 mini s2 s3 "])

    def test_inventory_standalone_node_has_no_workers(self):
        m = self.machine(mac_m4)
        self.write_inventory(m, {
            "mini": {"NODE_LOCAL": 1, "NODE_ROLE": "node"},
            "s1": {"NODE_ROLE": "head", "NODE_SSH": "s1"},
            "s2": {"NODE_ROLE": "worker", "NODE_SSH": "s2"},
        })
        self.assertEqual(self.site(m)[:4], ["1", "", "", "mini"])

    def test_inventory_without_this_machine_drives_the_head(self):
        m = self.machine(mac_m4)
        self.write_inventory(m, {"s1": {"NODE_ROLE": "head", "NODE_SSH": "me@s1"},
                                 "s2": {"NODE_ROLE": "worker", "NODE_SSH": "s2"}})
        s = self.site(m)
        self.assertEqual((s[0], s[1], s[3], s[6]), ("0", "s2", "s1", "me@s1"))

    def test_inventory_worker_is_not_the_head(self):
        m = self.machine(dgx_spark)
        self.write_inventory(m, {"s2": {"NODE_LOCAL": 1, "NODE_ROLE": "worker"},
                                 "s1": {"NODE_ROLE": "head", "NODE_SSH": "s1"}})
        s = self.site(m)
        self.assertEqual((s[0], s[3], s[6]), ("0", "s1", "s1"))

    def test_node_files_refuse_anything_a_shell_could_run(self):
        m = self.machine(linux_4090x2)
        for bad in ["NODE_SSH='a;b'", "NODE_SSH='$(id)'", "NODE_SSH='`id`'", "NODE_SSH='a b'",
                    "NODE_RACK_DIR='x|y'", "NODE_HF_CACHE='a\\\"b'", "NOT_A_KEY=1"]:
            r = m.bash("inv_set n1 %s" % bad)
            self.assertNotEqual(r.returncode, 0, bad)
        for bad in ["-x", "a/b", "a b", ".hidden", ""]:
            r = m.bash("inv_set '%s' NODE_ROLE=node" % bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(os.path.exists(m.config_path("nodes", "n1.env")))

    def test_rack_dir_defaults_to_this_checkouts_place(self):
        m = self.machine(linux_4090x2)
        r = m.bash('RACK_ROOT=$HOME/dgx-serve; inv_set w NODE_ROLE=worker; node_rack_dir w; echo; '
                   'inv_set w NODE_RACK_DIR=opt/rack; node_rack_dir w; echo; node_hf_cache w', {"HF_CACHE": "/data/hf"})
        self.assertEqual(r.stdout.split("\n"), ["dgx-serve", "opt/rack", "/data/hf"])

    def test_inventory_json(self):
        m = self.machine(linux_4090x2)
        self.write_inventory(m, {"a": {"NODE_LOCAL": 1, "NODE_ROLE": "head", "NODE_PLATFORM": "linux"}})
        d = rack_json(m.rack("nodes", "ls", "--json"))
        self.assertEqual(d["schema"], 1)
        self.assertEqual(d["nodes"][0]["name"], "a")
        self.assertEqual(d["nodes"][0]["role"], "head")
        self.assertEqual(sorted(d["nodes"][0]), sorted(["name", "ssh", "platform", "role", "local", "fabric_ip",
                                                        "fabric_if", "ib_hcas", "gpus", "rack_dir", "hf_cache"]))


class Nodes(Base):
    def head(self):
        m = self.machine(dgx_spark, name="s1", ips=[("f0", "10.9.0.1")])
        self.ok(m.rack("init", "--name", "s1"))
        return m

    def test_add_a_worker_by_fabric_probes_its_platform(self):
        m = self.head()
        self.machine(dgx_spark, host="s2", name="s2")
        self.ok(m.rack("nodes", "add", "s2", "--fabric", "10.9.0.2", "--fabric-if", "f0"))
        n = node_file(m, "s2")
        self.assertEqual((n["NODE_ROLE"], n["NODE_PLATFORM"], n["NODE_SSH"], n["NODE_FABRIC_IP"], n["NODE_GPUS"]),
                         ("worker", "dgx", "s2", "10.9.0.2", "1"))
        # adding again changes only what was given
        self.ok(m.rack("nodes", "add", "s2", "--hcas", "rdma0"))
        n = node_file(m, "s2")
        self.assertEqual((n["NODE_FABRIC_IP"], n["NODE_IB_HCAS"]), ("10.9.0.2", "rdma0"))

    def test_add_a_mac_is_a_standalone_node(self):
        m = self.head()
        self.machine(mac_m4, host="me@mini", name="mini")
        out = self.ok(m.rack("nodes", "add", "mini", "--ssh", "me@mini")).stdout
        n = node_file(m, "mini")
        self.assertEqual((n["NODE_ROLE"], n["NODE_PLATFORM"], n["NODE_SSH"]), ("node", "mac", "me@mini"))
        self.assertIn("ssh me@mini", out)

    def test_add_unreachable_needs_a_platform_and_says_how(self):
        m = self.head()
        r = m.rack("nodes", "add", "ghost")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ssh-copy-id ghost", r.stderr)
        self.ok(m.rack("nodes", "add", "ghost", "--linux", "--no-probe"))
        self.assertEqual(node_file(m, "ghost")["NODE_PLATFORM"], "linux")

    def test_add_refuses_what_cannot_work(self):
        m = self.head()
        self.machine(mac_m4, host="mini", name="mini")
        self.machine(linux_4090x2, host="pc", name="pc")
        cases = [
            (("nodes", "add", "pc", "--role", "head"), "already the head"),
            (("nodes", "add", "mini", "--role", "worker"), "same platform"),
            (("nodes", "add", "pc", "--fabric", "10.9.0.9"), "same platform"),
            (("nodes", "add", "mini", "--linux"), "says it is mac"),
            (("nodes", "add", "x", "--ssh", "a;b", "--linux", "--no-probe"), "shell characters"),
            (("nodes", "add", "x", "--fabric", "10.9.0", "--dgx", "--no-probe"), "IPv4"),
            (("nodes", "add", "s1", "--dgx", "--no-probe"), "this machine"),
            (("nodes", "add", "bad/name", "--dgx", "--no-probe"), "node names"),
        ]
        for args, msg in cases:
            r = m.rack(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(msg, r.stderr, args)
        self.assertEqual(sorted(os.listdir(m.config_path("nodes"))), ["s1.env"])

    def test_rm(self):
        m = self.head()
        self.ok(m.rack("nodes", "add", "w", "--dgx", "--no-probe", "--fabric", "10.9.0.2"))
        self.ok(m.rack("nodes", "rm", "w"))
        self.assertIsNone(node_file(m, "w"))
        self.assertNotEqual(m.rack("nodes", "rm", "w").returncode, 0)

    def test_test_reports_each_node_and_fails_on_any(self):
        m = self.head()
        m.cmd("ping", "exit 0")
        w = self.machine(dgx_spark, host="s2", name="s2", ips=[("f0", "10.9.0.2")])
        rack_dir = os.path.join(w.home, "dgx-serve")
        os.makedirs(rack_dir)
        os.symlink(os.path.join(ROOT, "rack"), os.path.join(rack_dir, "rack"))
        self.machine(dgx_spark, host="s3", name="s3")       # never got rack, has no fabric address
        down = self.machine(mac_m4, host="mini", name="mini")
        self.ok(m.rack("nodes", "add", "s2", "--fabric", "10.9.0.2", "--rack-dir", "dgx-serve"))
        self.ok(m.rack("nodes", "add", "s3", "--fabric", "10.9.0.3", "--rack-dir", "dgx-serve"))
        self.ok(m.rack("nodes", "add", "mini"))
        self.net.down(down)
        r = m.rack("nodes", "test", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        d = json.loads(r.stdout)
        got = {n["name"]: (n["ok"], n["ssh"], n["platform_check"], n["rack"], n["fabric"]) for n in d["nodes"]}
        self.assertFalse(d["ok"])
        self.assertEqual(got["s1"], (True, "local", "ok", "ok", "ok"))
        self.assertEqual(got["s2"], (True, "ok", "ok", "ok", "ok"))
        self.assertEqual(got["s3"], (False, "ok", "ok", "missing", "missing"))
        self.assertEqual(got["mini"], (False, "fail", "unknown", "unknown", "none"))
        self.assertEqual(self.ok(m.rack("nodes", "test", "s1", "s2")).returncode, 0)
        human = m.rack("nodes", "test").stdout
        self.assertIn("ssh-copy-id mini", human)


class Init(Base):
    def test_authors_rack_imports_spark_1_and_spark_2(self):
        m = self.machine(dgx_spark, name="burhan", ips=[("enp1s0f0np0", "192.168.100.1")])
        self.machine(dgx_spark, host="spark-2", name="aleem", ips=[("enp1s0f0np0", "192.168.100.2")])
        r = self.ok(m.rack("init"))
        h, w = node_file(m, "spark-1"), node_file(m, "spark-2")
        self.assertEqual((h["NODE_ROLE"], h["NODE_LOCAL"], h["NODE_PLATFORM"], h["NODE_FABRIC_IP"],
                          h["NODE_FABRIC_IF"], h["NODE_IB_HCAS"]),
                         ("head", "1", "dgx", "192.168.100.1", "enp1s0f0np0", "rocep1s0f0,roceP2p1s0f0"))
        self.assertEqual((w["NODE_ROLE"], w["NODE_SSH"], w["NODE_PLATFORM"], w["NODE_FABRIC_IP"], w["NODE_FABRIC_IF"]),
                         ("worker", "spark-2", "dgx", "192.168.100.2", "enp1s0f0np0"))
        key = open(m.config_path("engine.key")).read().strip()
        self.assertGreaterEqual(len(key), 40)
        self.assertNotIn(key, r.stdout + r.stderr)
        self.assertEqual(open(m.config_path("engine.env")).read(), "VLLM_API_KEY=%s\n" % key)
        for f in ("engine.key", "engine.env"):
            self.assertEqual(os.stat(m.config_path(f)).st_mode & 0o777, 0o600, f)
        # after init the same answers come from the inventory
        self.assertEqual(self.site(m)[:6], ["1", "spark-2", "spark-2", "spark-1", "192.168.100.1", "192.168.100.2"])

    def test_env_is_imported_once_and_a_rerun_keeps_everything(self):
        m = self.machine(linux_4090x2, name="gpu1", ips=[("eth9", "10.0.0.1")])
        self.machine(linux_4090x2, host="admin@gpu2", name="gpu2")
        m.dotenv("HEAD_IP=10.0.0.1\nWORKER_SSH=admin@gpu2\nWORKER_IP=10.0.0.2\nIB_HCAS=mlx5_0\n")
        self.ok(m.rack("init"))
        h, w = node_file(m, "gpu1"), node_file(m, "gpu2")
        self.assertEqual((h["NODE_FABRIC_IP"], h["NODE_FABRIC_IF"], h["NODE_IB_HCAS"]), ("10.0.0.1", "eth9", "mlx5_0"))
        self.assertEqual((w["NODE_SSH"], w["NODE_FABRIC_IP"], w["NODE_PLATFORM"], w["NODE_GPUS"]),
                         ("admin@gpu2", "10.0.0.2", "linux", "2"))
        key = open(m.config_path("engine.key")).read()
        self.ok(m.rack("nodes", "rm", "gpu2"))
        self.ok(m.rack("init"))
        self.assertIsNone(node_file(m, "gpu2"))                 # not re-imported
        self.assertEqual(open(m.config_path("engine.key")).read(), key)

    def test_single_box_json(self):
        m = self.machine(linux_4090x2, name="gpu-box")
        m.dotenv("WORKER_SSH=\n")
        m.cmd("docker", 'case "$*" in "info --format"*) echo \'{"runc":{}}\';; info) exit 0;; esac')
        m.cmd("loginctl", "echo yes")
        d = rack_json(m.rack("init", "--json"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["node"], "gpu-box")
        self.assertEqual(d["created"], ["gpu-box"])
        self.assertEqual(d["platform"]["platform"], "linux")
        self.assertEqual([n["name"] for n in d["inventory"]["nodes"]], ["gpu-box"])
        checks = {c["check"]: c["status"] for c in d["checks"]}
        self.assertEqual(checks["docker"], "ok")
        self.assertEqual(checks["NVIDIA container toolkit"], "ok")
        self.assertEqual(checks["linger"], "ok")
        self.assertNotIn("ssh", " ".join(checks))

    def test_failed_prerequisites_are_listed_with_their_fix(self):
        m = self.machine(dgx_spark, name="spark")
        m.cmd("docker", "exit 1")
        r = m.rack("init")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  docker", r.stdout)
        self.assertIn("usermod -aG docker", r.stdout)
        self.assertIsNotNone(node_file(m, "spark"))         # written anyway; a rerun keeps it

    def test_mac(self):
        m = self.machine(mac_m4, name="mini", memsize=68719476736)
        d = rack_json(m.rack("init", "--json"))
        self.assertEqual((d["node"], d["platform"]["platform"]), ("mini", "mac"))
        n = node_file(m, "mini")
        self.assertEqual((n["NODE_PLATFORM"], n["NODE_ROLE"], n["NODE_FABRIC_IP"]), ("mac", "head", ""))
        self.assertEqual({c["check"]: c["status"] for c in d["checks"]}["runtime"], "ok")

    def test_refuses_what_it_cannot_serve_on(self):
        m = self.machine(linux_no_gpu)
        r = m.rack("init")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no NVIDIA GPU", r.stderr)
        self.assertNotEqual(m.rack("init", "--force").returncode, 0)
        self.assertFalse(os.path.exists(m.config_path("nodes")))

    def test_unsupported_version_needs_force(self):
        m = self.machine(linux_4090x2)
        m.file("/etc/os-release", 'PRETTY_NAME="Ubuntu 20.04.6 LTS"\nVERSION_ID="20.04"\n')
        r = m.rack("init")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Ubuntu 22.04 or 24.04", r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertEqual(m.rack("init", "--force").returncode in (0, 1), True)
        self.assertEqual(node_file(m, "box")["NODE_PLATFORM"], "linux")

    def test_refuses_on_a_worker_or_a_laptop(self):
        w = self.machine(dgx_spark, ips=[("enp1s0f0np0", "192.168.100.2")])
        r = w.rack("init")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("worker of spark-1", r.stderr)
        lap = self.machine(mac_m4)
        r = lap.rack("init", extra_env={"HEAD_SSH": "gpu1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("drives gpu1", r.stderr)

    def test_joins_an_inventory_whose_head_is_elsewhere_as_a_node(self):
        m = self.machine(mac_m4, name="mini")
        self.ok(m.bash("inv_set s1 NODE_ROLE=head NODE_SSH=s1 NODE_PLATFORM=dgx"))
        self.ok(m.rack("init"))
        self.assertEqual(node_file(m, "mini")["NODE_ROLE"], "node")
        self.assertEqual(self.site(m)[:4], ["1", "", "", "mini"])


class Commands(Base):
    """The single-node bugs: every command works on one machine, and reaches
    every worker of many."""

    def two_workers(self):
        m = self.machine(dgx_spark, name="s1", ips=[("f0", "10.9.0.1")])
        self.ok(m.rack("init", "--name", "s1"))
        ws = []
        for i in (2, 3):
            w = self.machine(dgx_spark, host="admin@s%d" % i, name="s%d" % i, ips=[("f0", "10.9.0.%d" % i)])
            self.ok(m.rack("nodes", "add", "s%d" % i, "--ssh", "admin@s%d" % i, "--fabric", "10.9.0.%d" % i,
                           "--rack-dir", "opt/dgx-serve"))
            ws.append(w)
        os.remove(os.path.join(m.home, "ssh.log"))
        return m, ws

    def script(self, m, name, *args):
        import subprocess
        return subprocess.run(["/bin/bash", os.path.join(ROOT, "scripts", name)] + list(args), cwd=ROOT,
                              env=m.env(), capture_output=True, text=True, timeout=60)

    def test_stop_on_one_machine_never_calls_ssh(self):
        m = self.machine(linux_4090x2)
        m.dotenv("WORKER_SSH=\n")
        self.ok(self.script(m, "stop-cluster.sh"))
        self.assertEqual(m.log("ssh"), [])
        self.assertEqual([l for l in m.log("docker") if not l.startswith("info")], ["stop serve_node serve_solo"])

    def test_stop_reaches_every_worker(self):
        m, ws = self.two_workers()
        self.ok(self.script(m, "stop-cluster.sh"))
        self.assertEqual([l.split(" docker ")[0].split()[-1] for l in m.log("ssh")], ["admin@s2", "admin@s3"])
        for w in ws:
            self.assertEqual([l for l in w.log("docker") if not l.startswith("info")], ["stop serve_node"])

    def test_build_defaults_to_community_like_rack_build(self):
        m = self.machine(linux_4090x2)
        self.ok(self.script(m, "build.sh"))
        self.assertTrue(any("BASE_IMAGE=eugr/spark-vllm:latest" in l for l in m.log("docker")), m.log("docker"))
        self.assertEqual(m.log("ssh"), [])

    def test_build_ships_the_image_to_every_worker(self):
        m, ws = self.two_workers()
        m.logging_cmd("docker", 'case "$1" in image) echo sha256:local;; save) echo IMAGE;; esac')
        r = self.ok(self.script(m, "build.sh"))
        for w in ws:
            self.assertIn("load", w.log("docker"))
        self.assertIn("Image on every node", r.stdout)

    def test_sync_model(self):
        m = self.machine(linux_4090x2)
        os.makedirs(os.path.join(m.home, "hf", "hub", "models--org--m"))
        env = "HF_CACHE=%s/hf\n" % m.home
        m.dotenv(env + "WORKER_SSH=\n")
        r = self.ok(self.script(m, "sync-model.sh", "org/m"))
        self.assertIn("nothing to replicate", r.stdout)
        self.assertEqual(m.log("rsync"), [])
        m2, ws = self.two_workers()
        os.makedirs(os.path.join(m2.home, "hf", "hub", "models--org--m"))
        m2.dotenv("HF_CACHE=%s/hf\n" % m2.home)
        self.ok(self.script(m2, "sync-model.sh", "org/m"))
        dests = [l.split()[-1] for l in m2.log("rsync")]
        self.assertEqual(dests, ["admin@s%d:%s/hf/hub/models--org--m/" % (i, m2.home) for i in (2, 3)])

    def test_logs_read_the_engine_not_a_file_nobody_writes(self):
        m = self.machine(linux_4090x2)
        m.logging_cmd("docker", 'case "$*" in "inspect serve_solo") exit 0;; inspect*) exit 1;; '
                                '*serve_solo) echo "engine says hi"; echo "Traceback" >&2;; esac')
        r = self.ok(m.rack("logs"))
        self.assertIn("engine says hi", r.stdout)
        self.assertIn("Traceback", r.stdout)                       # stderr is where crashes are
        self.assertEqual(m.log("docker"), ["inspect serve_node", "inspect serve_solo", "logs --tail 60 serve_solo"])
        m.logging_cmd("docker", "exit 1")
        m.logging_cmd("journalctl", 'echo "last run: out of memory"')
        r = self.ok(m.rack("logs"))
        self.assertIn("from journald", r.stdout)
        self.assertIn("out of memory", r.stdout)
        r = m.rack("logs", "worker")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no worker", r.stderr)

    def test_logs_of_a_worker(self):
        m, ws = self.two_workers()
        ws[1].logging_cmd("docker", 'echo "rank 1 ready"')
        r = self.ok(m.rack("logs", "s3"))
        self.assertIn("rank 1 ready", r.stdout)

    def test_verify_names_this_machine(self):
        m = self.machine(linux_4090x2, name="gpu-box")
        snap = os.path.join(m.home, "hf", "hub", "models--org--m", "snapshots", "abc")
        os.makedirs(snap)
        m.dotenv("HF_CACHE=%s/hf\nWORKER_SSH=\n" % m.home)
        r = self.ok(m.rack("verify", "org/m"))
        self.assertIn("gpu-box: ", r.stdout)
        self.assertNotIn("spark-1", r.stdout)

    def test_worker_commands_run_from_the_workers_checkout(self):
        m, ws = self.two_workers()
        for w in ws:
            d = os.path.join(w.home, "opt", "dgx-serve", "scripts")
            os.makedirs(d)
            with open(os.path.join(d, "netcheck.sh"), "w") as f:
                f.write('echo "netcheck on $(hostname) from $PWD head=$HEAD_IP me=$WORKER_IP if=$FABRIC_IF"\n')
        r = m.rack("net")
        # (the fake ssh lands in a symlinked home, so match the path's tail)
        self.assertRegex(r.stdout, r"netcheck on s2 from \S*/home/opt/dgx-serve head=10.9.0.1 me=10.9.0.2 if=f0")
        self.assertRegex(r.stdout, r"netcheck on s3 from \S*/home/opt/dgx-serve head=10.9.0.1 me=10.9.0.3 if=f0")

    def test_status_on_one_machine_mentions_no_phantom_worker(self):
        m = self.machine(linux_4090x2, name="gpu-box")
        m.cmd("free", 'echo "Mem: 125 10 100 0 15 110"')
        r = m.rack("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gpu-box  nothing serving", r.stdout)
        self.assertNotIn("spark", r.stdout + r.stderr)
        self.assertEqual(m.log("ssh"), [])


if __name__ == "__main__":
    unittest.main()
