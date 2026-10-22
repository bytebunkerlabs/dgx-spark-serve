"""rack keep: an engine that stays dead is served again, never while it loads,
never over and over, and never after rack down.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

from helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "py"))
import keep  # noqa: E402

T = 1_800_000_000


class Decide(unittest.TestCase):
    def go(self, st, now, serving="glm", healthy=False, busy=False):
        return keep.decide(st, now, serving, healthy, busy, grace_s=900, gap_s=1800, max_per_day=3)

    def test_a_dead_engine_is_served_again_after_the_grace(self):
        a, st, _ = self.go({}, T)
        self.assertEqual((a, st["first_bad"]), ("none", T))             # first miss: it may be loading
        a, st, why = self.go(st, T + 600)
        self.assertEqual(a, "none", why)
        a, st, why = self.go(st, T + 900)
        self.assertEqual(a, "restart", why)
        self.assertEqual((st["restarts"], st["first_bad"]), ([T + 900], T + 900))   # its own load gets the grace

    def test_an_answer_clears_it(self):
        _, st, _ = self.go({}, T)
        a, st, _ = self.go(st, T + 300, healthy=True)
        self.assertNotIn("first_bad", st)
        a, st, _ = self.go(st, T + 1300)
        self.assertEqual((a, st["first_bad"]), ("none", T + 1300))

    def test_nothing_while_rack_up_runs_or_after_rack_down(self):
        _, st, _ = self.go({}, T)
        self.assertEqual(self.go(st, T + 5000, busy=True)[0], "none")
        a, st2, why = self.go(st, T + 5000, serving="")
        self.assertEqual(a, "none", why)
        self.assertNotIn("first_bad", st2)

    def test_at_most_once_per_gap_and_three_times_a_day_then_it_says_so(self):
        st, now, acts = {}, T, []
        for _ in range(200):                       # a check every 2 min for ~6.7 h, never answering
            a, st, _ = self.go(st, now)
            acts.append((a, now))
            now += 120
        restarts = [t for a, t in acts if a == "restart"]
        self.assertEqual(len(restarts), 3)
        self.assertTrue(all(b - a >= 1800 for a, b in zip(restarts, restarts[1:])))
        self.assertEqual([a for a, _ in acts].count("giveup"), 1)       # said once, not every 2 min
        # a day after the first restart, it may try again
        a, st, _ = self.go(st, restarts[0] + 86400 + 1)
        self.assertEqual(a, "restart")

    def test_another_recipe_starts_fresh(self):
        _, st, _ = self.go({}, T, serving="glm")
        a, st, _ = self.go(st, T + 1000, serving="qwen")
        self.assertEqual((a, st["first_bad"], st["recipe"]), ("none", T + 1000, "qwen"))


class LostShare(unittest.TestCase):
    """A worker that rebooted holds no engine while the head's /health still answers."""

    def test_a_lost_share_is_served_again_after_two_checks_not_fifteen_minutes(self):
        go = lambda st, now, lost: keep.decide(st, now, "glm", True, False, grace_s=900, gap_s=1800,
                                               max_per_day=3, lost=lost)
        a, st, why = go({}, T, ["spark-2"])
        self.assertEqual(a, "none", why)
        self.assertIn("spark-2's share of the engine is gone", why)
        a, st, why = go(st, T + 120, ["spark-2"])
        self.assertEqual(a, "restart", why)
        a, st, why = go(st, T + 240, [])                 # formed again: answers, nothing lost
        self.assertEqual((a, "first_bad" in st), ("none", False))

    def test_shares_and_monitors_are_asked_on_every_node(self):
        calls = []
        tops = {"": "UID PID CMD\nme 1 sleep infinity\nme 2 python3 /usr/bin/vllm serve x",
                "spark-2": "UID PID CMD\nme 1 sleep infinity"}

        def fake(ssh, command, timeout=30):
            calls.append((ssh, command))
            if command.startswith("docker top"):
                return (0, tops[ssh]) if ssh != "spark-3" else (None, "")
            if command.startswith("docker ps -a"):
                return 0, "rack-monitor exited\nrack-monitor-docker running\n" if ssh == "spark-2" else "rack-monitor running\n"
            return 0, ""
        saved = keep.on_node
        keep.on_node = fake
        try:
            rack = [("spark-1", ""), ("spark-2", "spark-2"), ("spark-3", "spark-3")]
            serving = {"runtime": "docker", "nodes": ["spark-1", "spark-2", "spark-3"]}
            self.assertEqual(keep.lost_shares(serving, rack, "serve_node"), ["spark-2"])   # spark-3 unreachable: not lost
            self.assertEqual(keep.lost_shares(dict(serving, nodes=["spark-1"]), rack, "serve_node"), [])   # solo
            self.assertEqual(keep.start_monitors(rack[:2]), ["rack-monitor on spark-2"])
            self.assertIn(("spark-2", "docker start rack-monitor >/dev/null 2>&1"), calls)
        finally:
            keep.on_node = saved

    def test_nodes_from_rack(self):
        os.environ["RACK_NODES"] = "spark-1\t\t192.168.100.1\nspark-2\tspark-2\t192.168.100.2\n"
        try:
            self.assertEqual(keep.nodes(), [("spark-1", ""), ("spark-2", "spark-2")])
        finally:
            del os.environ["RACK_NODES"]


class Cron(unittest.TestCase):
    def test_its_block_and_nothing_else(self):
        mine = "0 3 * * * backup.sh\n# a note\n"
        on = keep.with_block(mine, "*/2 * * * * rack keep")
        self.assertTrue(on.startswith(mine))
        self.assertIn(keep.BEGIN + "\n*/2 * * * * rack keep\n" + keep.END + "\n", on)
        again = keep.with_block(on, "*/2 * * * * rack keep")
        self.assertEqual(again, on)                                    # on twice: one block
        self.assertEqual(keep.with_block(on, None), mine)              # off: exactly what was there
        self.assertEqual(keep.with_block("", None), "")

    def test_the_line_finds_rack_and_docker_without_a_login(self):
        line = keep.cron_line("/home/me/dgx/dgx-serve/rack")
        self.assertTrue(line.startswith("*/2 * * * * PATH="))
        self.assertIn("/usr/bin", line)
        self.assertTrue(line.endswith("/home/me/dgx/dgx-serve/rack keep >/dev/null 2>&1"))


class State(unittest.TestCase):
    def test_reads_serving_json_and_keeps_its_log(self):
        d = tempfile.mkdtemp()
        k = keep.Keep(d)
        self.assertIsNone(k.serving())
        with open(os.path.join(d, "serving.json"), "w") as f:
            json.dump({"recipe": "glm", "port": 8888}, f)
        self.assertEqual(k.serving()["recipe"], "glm")
        k.save({"restarts": [1]})
        self.assertEqual(k.load(), {"restarts": [1]})
        k.log("glm: restarted")
        with open(k.log_path) as f:
            self.assertIn("glm: restarted", f.read())


if __name__ == "__main__":
    unittest.main()
