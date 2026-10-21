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
