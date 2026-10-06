"""rack gateway: LiteLLM routes kept between dgx-serve's markers, every other
line of a hand-kept config left exactly as it was.

    python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

from helpers import FakeMachine, ROOT, dgx_spark

sys.path.insert(0, os.path.join(ROOT, "py"))
import gateway  # noqa: E402

CONFIG = """# LiteLLM for the rack. Hand-kept: keep the comments.
model_list:
    # the cloud fallback
    - model_name: claude
      litellm_params:
        model: anthropic/claude-x
        api_key: os.environ/ANTHROPIC_API_KEY

    - model_name: "glm53"   # the big one
      litellm_params:
        model: openai/glm53
        api_base: http://172.19.0.1:8888/v1

litellm_settings:
  drop_params: true
"""


class Editor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "litellm.yaml")
        with open(self.path, "w") as f:
            f.write(CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def text(self):
        return open(self.path).read()

    def test_add_refresh_remove_keep_every_other_line(self):
        self.assertEqual(gateway.sync(self.path, "qwen3-8b", "qwen3-8b", "http://10.0.0.1:8888/v1", "none"), "changed")
        t = self.text()
        self.assertIn("    # >>> dgx-serve managed", t)                     # the list's own indentation
        self.assertIn("    - model_name: qwen3-8b\n      litellm_params:\n        model: openai/qwen3-8b\n"
                      "        api_base: http://10.0.0.1:8888/v1\n        api_key: none\n    # <<< dgx-serve managed", t)
        self.assertEqual(gateway.sync(self.path, "qwen3-8b", "qwen3-8b", "http://10.0.0.1:8888/v1", "none"), "unchanged")
        self.assertEqual(gateway.sync(self.path, "qwen3-8b", "qwen3-8b", "http://10.0.0.1:9000/v1", "none"), "changed")
        self.assertIn(":9000/v1", self.text())
        self.assertEqual(open(self.path + ".bak").read().count(":8888/v1\n        api_key: none"), 1)
        self.assertEqual(gateway.remove(self.path, "qwen3-8b"), "changed")
        without = [l for l in self.text().split("\n") if "dgx-serve managed" not in l]
        self.assertEqual([l for l in without if l], [l for l in CONFIG.split("\n") if l])

    def test_a_hand_route_is_never_shadowed_unless_adopted(self):
        with self.assertRaises(gateway.GatewayError) as e:
            gateway.sync(self.path, "glm53", "glm53", "http://x/v1", "none")
        self.assertIn("rack gateway adopt glm53", str(e.exception))
        self.assertEqual(gateway.adopt(self.path, "glm53"), "changed")
        st = gateway.status(self.path)
        self.assertEqual((st["managed"], st["by_hand"]), (["glm53"], ["claude"]))
        self.assertIn('- model_name: "glm53"   # the big one', self.text())     # moved as written
        self.assertEqual(gateway.sync(self.path, "glm53", "glm53", "http://10.0.0.1:8888/v1", "none"), "changed")

    def test_damaged_markers_are_left_to_a_human(self):
        with open(self.path, "a") as f:
            f.write("  " + gateway.END + "\n")
        with self.assertRaises(gateway.GatewayError):
            gateway.sync(self.path, "x", "x", "http://x/v1", "none")


class Rack(unittest.TestCase):
    def setUp(self):
        self.m = FakeMachine()
        dgx_spark(self.m)
        self.m.hostname("box")
        self.m.ips(("f0", "10.9.0.1"))
        self.cfg = os.path.join(self.m.home, "litellm.yaml")
        with open(self.cfg, "w") as f:
            f.write(CONFIG)
        self.m.dotenv("WORKER_SSH=\nHEAD_IP=10.9.0.1\nGATEWAY_CONFIG=%s\nGATEWAY_CONTAINER=litellm\n" % self.cfg)
        self.m.logging_cmd("docker")
        state = os.path.join(self.m.home, ".local", "state", "dgx-serve")
        os.makedirs(state)
        with open(os.path.join(state, "serving.json"), "w") as f:      # as rack up records it
            json.dump({"recipe": "qwen3-8b", "served_name": "qwen3-8b", "gateway_name": "qwen", "port": 8888,
                       "key_required": True, "runtime": "docker", "nodes": ["box"]}, f)

    def tearDown(self):
        self.m.cleanup()

    def test_sync_status_and_down(self):
        r = self.m.rack("gateway", "sync")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        t = open(self.cfg).read()
        self.assertIn("- model_name: qwen\n      litellm_params:\n        model: openai/qwen3-8b\n"
                      "        api_base: http://10.9.0.1:8888/v1\n        api_key: os.environ/DGX_SERVE_ENGINE_KEY", t)
        self.assertIn("DGX_SERVE_ENGINE_KEY", r.stdout)
        self.assertEqual(self.m.log("docker"), ["restart litellm"])
        self.assertIn("rack keeps qwen", self.m.rack("gateway").stdout)
        r = self.m.rack("down")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("model_name: qwen\n", open(self.cfg).read())
        self.assertEqual(self.m.log("docker").count("restart litellm"), 2)

    def test_off_without_a_config(self):
        self.m.dotenv("WORKER_SSH=\n")
        self.assertIn("no gateway", self.m.rack("gateway").stdout)
        self.assertEqual(self.m.rack("down").returncode, 0)
        self.assertNotIn("restart litellm", self.m.log("docker"))


if __name__ == "__main__":
    unittest.main()
