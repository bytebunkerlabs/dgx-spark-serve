"""rack monitor, as rack drives it: bare under launchd on a Mac, and in
containers on every worker of the inventory.

    python3 -m unittest discover -s tests
"""
import os
import plistlib
import unittest

from helpers import FakeMachine, FakeNet, ROOT, dgx_spark, mac_m4


class Base(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.machines = []

    def tearDown(self):
        for m in self.machines:
            m.cleanup()
        self.net.cleanup()

    def machine(self, build, host=None, name="box", ips=()):
        m = FakeMachine()
        self.machines.append(m)
        build(m)
        m.hostname(name)
        m.ips(*ips)
        self.net.add(m, *([host] if host else []))
        return m

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r


ENV = {"MONITOR_WAIT_S": "0", "MONITOR_PORT": "19999"}


class Bare(Base):
    def test_a_mac_runs_it_under_launchd(self):
        m = self.machine(mac_m4, name="mini")
        m.logging_cmd("launchctl", 'case "$1" in print) exit 113;; esac')
        r = self.ok(m.rack("monitor", "up", extra_env=ENV))
        self.assertIn("started (launchd, ai.bytebunker.dgx-serve.monitor)", r.stdout)
        plist = os.path.join(m.home, "Library", "LaunchAgents", "ai.bytebunker.dgx-serve.monitor.plist")
        job = plistlib.load(open(plist, "rb"))
        self.assertEqual(job["ProgramArguments"][1:], [os.path.join(ROOT, "monitor", "rackmon.py"), "serve"])
        env = job["EnvironmentVariables"]
        self.assertEqual((env["MONITOR_NAME"], env["MONITOR_ROLE"], env["MONITOR_PEERS"], env["MONITOR_PORT"]),
                         ("mini", "head", "", "19999"))
        self.assertEqual(env["MONITOR_SERVING"], os.path.join(m.home, ".local", "state", "dgx-serve", "serving.json"))
        self.assertEqual(env["MONITOR_ENGINE_KEY_FILE"], m.config_path("engine.key"))
        token = os.path.join(m.home, ".config", "rack", "monitor.token")
        self.assertEqual(os.stat(token).st_mode & 0o777, 0o600)
        self.assertNotIn(open(token).read().strip(), r.stdout + r.stderr)
        calls = m.log("launchctl")
        self.assertTrue(calls.index("bootout gui/%d/ai.bytebunker.dgx-serve.monitor" % os.getuid())
                        < calls.index("bootstrap gui/%d %s" % (os.getuid(), plist)))
        self.ok(m.rack("monitor", "down", extra_env=ENV))
        self.assertFalse(os.path.exists(plist))
        self.assertTrue(os.path.exists(token))                         # kept for the next up


class Docker(Base):
    def test_every_worker_of_the_inventory_gets_one(self):
        fake_docker = ('case "$1 $2" in "image inspect") echo sha256:monitor;; esac; '
                       'case "$1" in save) echo IMAGE;; esac')
        head = self.machine(dgx_spark, name="s1", ips=[("f0", "10.9.0.1")])
        self.ok(head.rack("init", "--name", "s1"))
        workers = []
        for i in (2, 3):
            w = self.machine(dgx_spark, host="s%d" % i, name="s%d" % i, ips=[("f0", "10.9.0.%d" % i)])
            self.ok(head.rack("nodes", "add", "s%d" % i, "--fabric", "10.9.0.%d" % i))
            workers.append(w)
        for m in [head] + workers:
            m.logging_cmd("docker", fake_docker)
            m.cmd("stat", 'echo 999')                                  # stat -c %g on the docker socket
        r = self.ok(head.rack("monitor", "up", extra_env=ENV))
        runs = [c for c in head.log("docker") if c.startswith("run -d --name rack-monitor ")]
        self.assertEqual(len(runs), 1, head.log("docker"))
        self.assertIn("MONITOR_PEERS=s2=http://10.9.0.2:19999,s3=http://10.9.0.3:19999", runs[0])
        self.assertIn("MONITOR_SERVING=/run/dgx-serve/serving.json", runs[0])
        for w, ip in zip(workers, ("10.9.0.2", "10.9.0.3")):
            wruns = [c for c in w.log("docker") if c.startswith("run -d --name rack-monitor ")]
            self.assertEqual(len(wruns), 1, w.log("docker"))
            self.assertIn("MONITOR_BIND=%s,127.0.0.1" % ip, wruns[0])
            self.assertIn("MONITOR_ROLE=worker", wruns[0])
            self.assertTrue(os.path.exists(os.path.join(w.home, ".config", "rack", "monitor.token")))
        self.assertIn("monitor: s3", r.stdout)
        self.ok(head.rack("monitor", "down", extra_env=ENV))
        for w in workers:
            self.assertIn("rm -f rack-monitor rack-monitor-docker", w.log("docker"))


if __name__ == "__main__":
    unittest.main()
