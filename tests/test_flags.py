"""Platform flags (--dgx --linux --windows --mac), --plan, --json and --on,
and how a recipe name finds its variant for a platform.

    python3 -m unittest discover -s tests
"""
import json
import os
import unittest

from helpers import FakeMachine, FakeNet, dgx_spark, linux_4090x2, mac_m4, wsl_2070, rack_json

REFUSAL = ("this is a DGX Spark; --mac needs a Mac: run it there, or add the Mac with rack nodes add "
           "and use --on. --plan shows what would run, on any machine.")


class Base(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.machines = []

    def tearDown(self):
        for m in self.machines:
            m.cleanup()
        self.net.cleanup()

    def machine(self, build, host=None, name="box", ips=(), **kw):
        m = FakeMachine()
        self.machines.append(m)
        build(m, **kw)
        m.hostname(name)
        m.ips(*ips)
        self.net.add(m, *([host] if host else []))
        return m

    def recipe(self, m, name, files):
        """A v2 recipe in the machine's own recipe folder (the overlay)."""
        d = m.config_path("recipes", name)
        os.makedirs(d, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)

    def fails(self, proc, text):
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(text, proc.stderr)


TINY = {
    "model.env": "MODEL=Qwen/Qwen3-0.6B\nROLES=chat\n",
    "mac.env": '. "$RECIPE_DIR/model.env"\nENGINE=llamacpp\nARTIFACT=Qwen/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q8_0.gguf\n'
               'SERVE_ARGS=(--ctx-size 8192)\n',
}


class Flags(Base):
    def test_parse(self):
        m = self.machine(linux_4090x2)
        r = m.bash('flags_parse up x --mac --plan -- --json --dgx; echo "$WANT_PLATFORM|$PLAN|$JSON|${REST[*]}"')
        self.assertEqual(r.stdout.strip(), "mac|1|0|up x --json --dgx")
        r = m.bash("flags_parse x --mac --dgx")
        self.assertIn("one platform at a time", r.stderr)
        r = m.bash('set -u; flags_parse --plan; echo "n=${#REST[@]}" ${REST[@]+"${REST[@]}"}')
        self.assertEqual(r.stdout.strip(), "n=0")

    def test_mismatch_is_refused_with_the_reason_and_the_fix(self):
        m = self.machine(dgx_spark)
        for cmd in (("up", "phase1-qwen3-8b", "--mac"), ("pull", "Qwen/Qwen3-8B", "--mac"),
                    ("fit", "Qwen/Qwen3-8B", "--mac")):
            r = m.rack(*cmd)
            self.fails(r, REFUSAL)

    def test_refusal_names_the_node_that_can(self):
        m = self.machine(dgx_spark, name="s1")
        self.assertEqual(m.rack("init").returncode, 0)
        self.assertEqual(m.rack("nodes", "add", "mini", "--mac", "--no-probe").returncode, 0)
        self.fails(m.rack("up", "phase1-qwen3-8b", "--mac"), "or use --on mini (rack up ... --on mini)")

    def test_no_flag_means_this_machine(self):
        m = self.machine(dgx_spark)
        d = rack_json(m.rack("up", "phase1", "--plan", "--json"))
        self.assertEqual((d["recipe"], d["platform"], d["model"], len(d["nodes"])),
                         ("phase1-qwen3-8b", "dgx", "Qwen/Qwen3-8B", 1))
        self.assertTrue(d["variant"].endswith("/recipes/phase1-qwen3-8b/dgx.env"), d["variant"])

    def test_a_flat_recipe_serves_dgx_and_linux_only(self):
        m = self.machine(linux_4090x2)
        flat = "phase2-gpt-oss-120b-solo"
        self.assertEqual(rack_json(m.rack("up", flat, "--plan", "--json"))["platform"], "linux")
        r = m.rack("up", flat, "--windows", "--plan")
        self.fails(r, flat + " is a vLLM container recipe from before 1.0")
        self.assertIn("rack new %s --windows" % flat, r.stderr)

    def test_plan_works_on_any_machine(self):
        m = self.machine(dgx_spark)
        self.recipe(m, "tiny", TINY)
        d = rack_json(m.rack("up", "tiny", "--mac", "--plan", "--json"))
        self.assertEqual((d["platform"], d["model"]), ("mac", "Qwen/Qwen3-0.6B"))
        self.assertTrue(d["variant"].endswith("/recipes/tiny/mac.env"))
        self.assertIn("tiny --mac (nothing was run)", m.rack("up", "tiny", "--mac", "--plan").stdout)

    def test_missing_variant_says_which_exist_and_how_to_add(self):
        m = self.machine(mac_m4)
        self.recipe(m, "tiny", TINY)
        self.fails(m.rack("up", "tiny", "--dgx", "--plan"),
                   "tiny has no dgx variant (it has: mac). Add one: rack new tiny --dgx")

    def test_two_node_recipe_plans_across_the_rack(self):
        m = self.machine(dgx_spark, ips=[("enp1s0f0np0", "192.168.100.1")])
        d = rack_json(m.rack("up", "phase2-gpt-oss-120b", "--plan", "--json"))
        self.assertEqual([n["name"] for n in d["nodes"]], ["spark-1", "spark-2"])

    def test_names_prefixes_and_the_overlay(self):
        m = self.machine(dgx_spark)
        self.fails(m.rack("up", "phase2", "--plan"), "ambiguous: phase2 matches phase2-gpt-oss-120b phase2-gpt-oss-120b-solo")
        self.fails(m.rack("up", "nope", "--plan"), "no such recipe: nope")
        self.fails(m.rack("up", "TEMPLATE", "--plan"), "the scaffold, not a recipe")
        # your own recipe of the same name wins over the checkout's
        self.recipe(m, "phase1-qwen3-8b", {"model.env": "MODEL=me/mine\n", "dgx.env": '. "$RECIPE_DIR/model.env"\n'})
        d = rack_json(m.rack("up", "phase1-qwen3-8b", "--plan", "--json"))
        self.assertEqual(d["model"], "me/mine")

    def test_recipes_lists_platforms_and_filters(self):
        m = self.machine(mac_m4)
        self.recipe(m, "tiny", TINY)
        out = m.rack("recipes").stdout
        self.assertRegex(out, r"tiny\s+solo\s+mac\s+Qwen/Qwen3-0.6B")
        self.assertRegex(out, r"phase2-gpt-oss-120b-solo\s+solo\s+dgx,linux\s+openai/gpt-oss-120b")
        self.assertRegex(out, r"phase1-qwen3-8b\s+solo\s+dgx\s+Qwen/Qwen3-8B")
        self.assertNotIn("TEMPLATE", out)
        mac = m.rack("recipes", "--mac").stdout
        self.assertIn("tiny", mac)
        self.assertNotIn("phase1", mac)

    def test_pull_takes_a_recipe_or_a_repo(self):
        # (downloads themselves: test_hfget.py, against a fake hub)
        m = self.machine(dgx_spark)
        self.recipe(m, "localpath", {"model.env": "MODEL=/root/.cache/huggingface/local/some/model\n",
                                     "dgx.env": '. "$RECIPE_DIR/model.env"\nENGINE=vllm\nSERVE_ARGS=()\n'})
        self.fails(m.rack("pull", "localpath", "--plan"), "is not a Hugging Face repo")
        self.fails(m.rack("pull", "nope", "--plan"), "no such recipe: nope")
        self.fails(m.rack("pull", "phase1", "--plan"), "cannot reach http://127.0.0.1:9")

    def test_windows_needs_its_own_variant(self):
        m = self.machine(wsl_2070)
        self.recipe(m, "tiny", dict(TINY, **{"windows.env": '. "$RECIPE_DIR/model.env"\nENGINE=llamacpp\n'
                                             'ARTIFACT=Qwen/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q8_0.gguf\n'}))
        self.assertEqual(rack_json(m.rack("up", "tiny", "--plan", "--json"))["variant"].split("/")[-1], "windows.env")


class Version(Base):
    def test_version_json_is_what_the_app_and_on_check(self):
        m = self.machine(dgx_spark)
        d = rack_json(m.rack("version", "--json"))
        self.assertEqual((d["schema"], d["json_schema"], d["recipe_schema"], d["monitor_schema"]), (1, 1, 2, 1))
        self.assertEqual(d["engines"]["llama.cpp"], "b11430")
        self.assertIn('"json_schema":1,', m.rack("version", "--json").stdout)       # what --on greps for
        self.assertRegex(m.rack("version").stdout, r"^rack 1\.0\.0-dev")


class On(Base):
    def remote_rack(self, m, rel="dgx-serve", old=False):
        """A stand-in rack on a node that says where and how it was run, and
        logs every call. An old one knows no `version` (and no --plan)."""
        d = os.path.join(m.home, rel)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "rack")
        version = "" if old else 'if [ "$1" = version ]; then echo \'{"schema":1,"json_schema":1}\'; exit 0; fi\n'
        with open(p, "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "$HOME/rack-calls.log"\n' + version +
                    'case "$1" in version) echo "unknown command: version" >&2; exit 1;; esac\n'
                    'echo "rack on $(hostname) in $PWD:" "$@"\n')
        os.chmod(p, 0o755)

    def rack_with_mini(self):
        m = self.machine(dgx_spark, name="s1")
        mini = self.machine(mac_m4, host="me@mini", name="mini")
        self.remote_rack(mini)
        self.assertEqual(m.rack("init").returncode, 0)
        r = m.rack("nodes", "add", "mini", "--ssh", "me@mini", "--rack-dir", "dgx-serve")
        self.assertEqual(r.returncode, 0, r.stderr)
        return m, mini

    def test_on_runs_the_same_command_there(self):
        m, mini = self.rack_with_mini()
        r = m.rack("up", "qwen3-8b", "--mac", "--on", "mini", "--plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"rack on mini in \S*/dgx-serve: up qwen3-8b --mac --plan")
        r = m.rack("status", "--on=mini")
        self.assertIn(": status", r.stdout)

    def test_on_refuses_a_platform_the_node_is_not(self):
        m, _ = self.rack_with_mini()
        self.fails(m.rack("up", "x", "--dgx", "--on", "mini"), "mini is a Mac; --dgx needs a DGX Spark")
        self.assertEqual(m.rack("up", "x", "--dgx", "--on", "mini", "--plan").returncode, 0)

    def test_on_is_not_for_setting_up_a_machine(self):
        m, _ = self.rack_with_mini()
        self.fails(m.rack("init", "--on", "mini"), "rack init works on this machine only")
        self.fails(m.rack("nodes", "ls", "--on", "mini"), "works on this machine only")
        self.fails(m.rack("status", "--on", "ghost"), "no such node: ghost")
        self.fails(m.rack("status", "--on"), "--on needs a node name")

    def test_on_this_machine_runs_here(self):
        m, _ = self.rack_with_mini()
        d = rack_json(m.rack("up", "phase1", "--plan", "--json", "--on", "s1"))
        self.assertEqual(d["platform"], "dgx")

    def test_on_never_forwards_to_an_older_rack(self):
        # A rack from before 1.0 ignores --plan and would launch: refuse first.
        m, mini = self.rack_with_mini()
        self.remote_rack(mini, old=True)
        self.fails(m.rack("up", "qwen3-8b", "--plan", "--on", "mini"), "older than this one")
        self.assertEqual(mini.log("rack-calls"), ["version --json"])

    def test_on_a_node_without_rack(self):
        m, mini = self.rack_with_mini()
        import shutil
        shutil.rmtree(os.path.join(mini.home, "dgx-serve"))
        self.fails(m.rack("status", "--on", "mini"), "rack is not installed at ~/dgx-serve on mini")


if __name__ == "__main__":
    unittest.main()
