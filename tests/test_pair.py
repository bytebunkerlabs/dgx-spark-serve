"""rack pair, unpair and remote: what the ByteBunker app reads to reach this
rack, its own ssh key restricted to rack's commands, and the forced command
that refuses everything else.

    python3 -m unittest discover -s tests
"""
import json
import os
import unittest

from helpers import FakeMachine, ROOT, dgx_spark, rack_json

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOmb3VoV1y3hGXq2Gc0rYgBqQ9Ff1yYqvGQnS8o8q7pZ bytebunker-app@hermes"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB2dQnE4nq7kq1wq3pX7rDq5r6s8tQyVx2nL4vG0aBcD app@laptop"
MINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC0nnOtHeRrSoWnKeYdOnTtOuChItAtAlLpLzZz me@laptop"


class Pair(unittest.TestCase):
    def setUp(self):
        m = self.m = FakeMachine()
        dgx_spark(m)
        m.hostname("box")
        m.ips(("f0", "10.9.0.1"))
        m.dotenv("WORKER_SSH=\nHEAD_IP=10.9.0.1\n")
        m.cmd("tailscale", 'case "$1" in ip) echo 100.64.0.7;; status) echo \'{"Self": {"DNSName": "box.tail.ts.net."}}\';; esac')
        os.makedirs(m.config_path(), exist_ok=True)
        with open(m.config_path("engine.key"), "w") as f:
            f.write("engine-secret-123\n")
        os.makedirs(os.path.join(m.home, ".config", "rack"))
        with open(os.path.join(m.home, ".config", "rack", "monitor.token"), "w") as f:
            f.write("monitor-secret-456\n")
        state = os.path.join(m.home, ".local", "state", "dgx-serve")
        os.makedirs(state)
        with open(os.path.join(state, "serving.json"), "w") as f:
            json.dump({"recipe": "qwen3-8b", "served_name": "qwen3-8b", "port": 8888, "key_required": True,
                       "runtime": "docker", "nodes": ["box"]}, f)
        self.auth = os.path.join(m.home, ".ssh", "authorized_keys")
        self.rack_path = os.path.join(ROOT, "rack")

    def tearDown(self):
        self.m.cleanup()

    def lines(self):
        return open(self.auth).read().splitlines() if os.path.exists(self.auth) else []

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_json_has_what_the_app_needs_and_a_person_sees_no_secrets(self):
        d = rack_json(self.m.rack("pair", "--json"))
        self.assertEqual((d["platform"], d["head"], d["schema"]), ("dgx", True, 1))
        self.assertEqual(d["engine"]["key"], "engine-secret-123")
        self.assertEqual((d["engine"]["port"], d["engine"]["serving"]["recipe"]), (8888, "qwen3-8b"))
        self.assertEqual((d["monitor"]["port"], d["monitor"]["token"]), (9177, "monitor-secret-456"))
        self.assertEqual(d["addresses"][0], {"ip": "100.64.0.7", "kind": "tailnet", "name": "box.tail.ts.net"})
        self.assertEqual(d["apps"], [])
        r = self.ok(self.m.rack("pair"))
        self.assertIn("serving qwen3-8b", r.stdout)
        for secret in ("engine-secret-123", "monitor-secret-456"):
            self.assertNotIn(secret, r.stdout + r.stderr)

    def test_the_apps_key_runs_rack_and_nothing_else(self):
        os.makedirs(os.path.dirname(self.auth))
        with open(self.auth, "w") as f:
            f.write(MINE + "\n")
        self.ok(self.m.rack("pair", "--key", KEY, "--name", "hermes"))
        body = KEY.split()[1]
        self.assertEqual(self.lines(), [MINE, 'command="%s remote",restrict ssh-ed25519 %s bytebunker:hermes' % (self.rack_path, body)])
        self.assertEqual(os.stat(self.auth).st_mode & 0o777, 0o600)
        # the same key again replaces its line; another key is a second app
        self.ok(self.m.rack("pair", "--key", KEY, "--name", "hermes-app"))
        self.ok(self.m.rack("pair", "--key", KEY2, "--name", "laptop"))
        self.assertEqual(sum(body in l for l in self.lines()), 1)
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual(rack_json(self.m.rack("pair", "--json"))["apps"], ["hermes-app", "laptop"])

    def test_anything_but_one_public_key_is_refused(self):
        for bad in (KEY + "\n" + KEY2, 'command="sh" ' + KEY, "no-pty " + KEY, "not a key",
                    KEY.replace("bytebunker-app", 'x"y'), KEY.replace(" ", "\t"), "ssh-ed25519 AAAA;rm"):
            r = self.m.rack("pair", "--key", bad, "--name", "x")
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("not one SSH public key", r.stderr)
        r = self.m.rack("pair", "--key", KEY, "--name", "two words")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.lines(), [])

    def test_unpair_removes_only_the_apps_lines(self):
        os.makedirs(os.path.dirname(self.auth))
        with open(self.auth, "w") as f:
            f.write(MINE + "\n")
        self.ok(self.m.rack("pair", "--key", KEY, "--name", "hermes"))
        self.ok(self.m.rack("pair", "--key", KEY2, "--name", "laptop"))
        self.assertIn("removed 1 app key (hermes)", self.ok(self.m.rack("unpair", "--name", "hermes")).stdout)
        self.assertEqual([l.split()[-1] for l in self.lines()], ["me@laptop", "bytebunker:laptop"])
        self.ok(self.m.rack("unpair"))
        self.assertEqual(self.lines(), [MINE])

    def remote(self, line):
        return self.m.rack("remote", extra_env={"SSH_ORIGINAL_COMMAND": line, "SSH_CLIENT": "100.64.0.9 50000 22"})

    def test_remote_runs_rack_commands_only(self):
        self.m.logging_cmd("touch")
        d = rack_json(self.remote("rack version --json"))
        self.assertEqual(d["json_schema"], 1)
        self.assertEqual(rack_json(self.remote("pair --json"))["engine"]["port"], 8888)
        for bad in ("status; touch /tmp/owned", "$(touch x)", "`touch x`", "status && touch x", "status | touch x",
                    "init", "nodes add evil --ssh x", "nodes rm box", "pair --json --key x", "pair",
                    "up qwen3-8b --on spark-2", "monitor token", "gateway remove qwen", "unpair", "remote",
                    "install", "new x y", "rack", "", "status\ntouch x", "logs\t-f"):
            r = self.remote(bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("rack remote:", r.stderr, bad)
        self.assertEqual(self.m.log("touch"), [])
        log = open(os.path.join(self.m.home, ".local", "state", "dgx-serve", "remote.log")).read().splitlines()
        self.assertTrue(log[0].endswith("100.64.0.9 ran rack version --json"), log[0])
        self.assertIn("100.64.0.9 refused status; touch /tmp/owned", log[2])

    def test_recipes_show_prints_the_files_a_recipe_runs(self):
        d = self.m.config_path("recipes", "tiny")
        os.makedirs(d)
        with open(os.path.join(d, "model.env"), "w") as f:
            f.write("MODEL=Qwen/Qwen3-0.6B\n")
        with open(os.path.join(d, "dgx.env"), "w") as f:
            f.write('. "$RECIPE_DIR/model.env"\nSERVE_ARGS=(--max-model-len 8192)\n')
        out = self.ok(self.remote("recipes show tiny")).stdout
        self.assertEqual(out, "# %s/model.env\nMODEL=Qwen/Qwen3-0.6B\n\n# %s/dgx.env\n"
                              '. "$RECIPE_DIR/model.env"\nSERVE_ARGS=(--max-model-len 8192)\n' % (d, d))


if __name__ == "__main__":
    unittest.main()
