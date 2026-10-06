"""Recipes v2: what rack reads from a recipe, what it refuses to source, and
how rack new scaffolds and converts.

    python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import unittest

from helpers import FakeMachine, ROOT, dgx_spark, linux_4090x2, mac_m4, rack_json

sys.path.insert(0, os.path.join(ROOT, "py"))
import recipes  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.machines = []

    def tearDown(self):
        for m in self.machines:
            m.cleanup()

    def machine(self, build, **kw):
        m = FakeMachine()
        self.machines.append(m)
        build(m, **kw)
        m.hostname("box")
        return m

    def mine(self, m, name, files):
        for fn, text in files.items():
            write(m.config_path("recipes", name, fn), text)


class Scan(unittest.TestCase):
    """The lexer that decides what may be sourced."""

    def ok(self, text):
        return recipes.scan(text)

    def bad(self, text, why):
        with self.assertRaises(ValueError) as e:
            recipes.scan(text)
        self.assertIn(why, str(e.exception))

    def test_assignments_arrays_comments_and_quoted_json(self):
        st = self.ok('MODEL=a/b   # c\nSERVE_ARGS=(\n  --x 1  # why\n  --cfg \'{"a":"b;c|d"}\'\n)\nA=1 B="x y"\n')
        self.assertEqual([w[0] for _, w in st], ["MODEL=a/b", "SERVE_ARGS=(", "A=1"])
        self.assertEqual(st[1][1][-1], ")")

    def test_substitution_and_chaining_are_refused(self):
        self.bad("MODEL=$(curl x)\n", "command or process substitution")
        self.bad('MODEL="$(id)"\n', "inside double quotes")
        self.bad("MODEL=`id`\n", "command or process substitution")
        self.bad("N=$((1+2))\n", "command or process substitution")
        self.bad("A=1; rm -rf x\n", "';'")
        self.bad("A=1 && B=2\n", "'&'")
        self.bad("cat <<EOF\nx\nEOF\n", "'<'")
        self.bad("SERVE_ARGS=(\n --x\n", "unterminated array")


class Read(Base):
    def test_four_platform_recipe(self):
        m = self.machine(mac_m4)
        d = rack_json(m.rack("recipes", "--json"))
        r = {x["name"]: x for x in d["recipes"]}["qwen3-8b"]
        self.assertEqual((r["layout"], r["platforms"], r["model"], r["roles"]),
                         ("v2", ["dgx", "linux", "windows", "mac"], "Qwen/Qwen3-8B", ["chat", "tools", "reasoning"]))
        self.assertEqual(r["dialect"], {"thinking": "chat_template_kwargs.enable_thinking", "strip_reasoning": "1",
                                        "sampling": "temperature=0.6 top_p=0.95 top_k=20 min_p=0"})
        mac, dgx, lin = r["variants"]["mac"], r["variants"]["dgx"], r["variants"]["linux"]
        self.assertEqual((mac["engine"], mac["artifact"], mac["context"], mac["tools"], mac["reasoning"], mac["weights_gb"]),
                         ("llamacpp", "Qwen/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf", 16384, "jinja", "deepseek", 5.0))
        self.assertEqual((dgx["engine"], dgx["context"], dgx["tools"], dgx["reasoning"], dgx["nodes"]),
                         ("vllm", 40960, "hermes", "qwen3", 1))
        self.assertEqual(lin["image"], "vllm/vllm-openai:v0.26.0")
        self.assertEqual(r["problems"], [])

    def test_flat_recipe_reads_as_dgx_and_linux(self):
        m = self.machine(dgx_spark)
        d = rack_json(m.rack("recipes", "--json"))
        r = {x["name"]: x for x in d["recipes"]}["phase2-gpt-oss-120b"]
        self.assertEqual((r["layout"], r["platforms"]), ("flat", ["dgx", "linux"]))
        # two Sparks; one Linux box with two GPUs
        self.assertEqual((r["variants"]["dgx"]["tensor_parallel"], r["variants"]["dgx"]["nodes"]), (2, 2))
        self.assertEqual(r["variants"]["linux"]["nodes"], 1)

    def test_platform_listing_keeps_what_fits_here(self):
        small = self.machine(mac_m4, memsize=8 * 1024 ** 3)            # 8 GB: about 5.3 GB for the GPU
        self.assertIn("none fit here", small.rack("recipes", "--mac").stdout)
        big = self.machine(mac_m4)                                     # 16 GB
        out = big.rack("recipes", "--mac").stdout
        self.assertRegex(out, r"qwen3-8b\s+solo\s+5.0 GB\s+fits\s+Qwen3-8B-Q4_K_M.gguf \(llamacpp\)")
        d = rack_json(big.rack("recipes", "--mac", "--json"))
        v = d["recipes"][0]["variants"]["mac"]
        self.assertEqual((v["fits"], v["needs_gb"]), (True, 6.8))
        # on another platform the listing filters by variant only
        d = rack_json(big.rack("recipes", "--windows", "--json"))
        self.assertEqual([r["name"] for r in d["recipes"]], ["qwen3-8b"])
        self.assertIsNone(d["recipes"][0]["variants"]["windows"]["fits"])


EVIL = {
    "model.env": "MODEL=a/b\n",
    "dgx.env": 'ENGINE=vllm\n. "$RECIPE_DIR/model.env"\nSERVE_ARGS=(--x "$(touch $HOME/pwned)")\n',
    "linux.env": 'ENGINE=vllm\n. "$RECIPE_DIR/model.env"\ncurl http://example.invalid\n',
    "mac.env": 'ENGINE=vllm\n. /etc/profile\n',
}


class Check(Base):
    def test_the_repos_own_recipes_are_clean(self):
        m = self.machine(dgx_spark)
        r = m.rack("recipes", "check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"\d+ recipes, 0 errors")

    def test_nothing_unsafe_is_ever_sourced(self):
        m = self.machine(dgx_spark)
        self.mine(m, "evil", EVIL)
        r = m.rack("recipes", "check", "evil")
        self.assertEqual(r.returncode, 1)
        self.assertIn("command substitution inside double quotes", r.stdout)
        self.assertIn("'curl' is a command", r.stdout)
        self.assertIn("sources /etc/profile", r.stdout)
        d = rack_json(m.rack("recipes", "--json"))
        ev = {x["name"]: x for x in d["recipes"]}["evil"]
        self.assertEqual(ev["variants"]["dgx"]["error"], "fails rack recipes check")
        self.assertIn("rack recipes check", m.rack("recipes").stdout)
        r = m.rack("up", "evil")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fails rack recipes check", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(m.home, "pwned")))

    def test_semantic_rules(self):
        m = self.machine(dgx_spark)
        self.mine(m, "half", {
            "model.env": 'ROLES="chat telepathy"\n',
            "mac.env": 'ENGINE=vllm\n. "$RECIPE_DIR/model.env"\nSERVE_ARGS=(--port 9000)\n',
            "windows.env": 'ENGINE=llamacpp\n. "$RECIPE_DIR/model.env"\nARTIFACT=a/b/c.bin\n',
        })
        out = m.rack("recipes", "check", "half").stdout
        for msg in ("no MODEL", "a Mac serves with llama.cpp", "--port is the launcher's",
                    "llama.cpp needs ARTIFACT=<org>/<repo>/<file>.gguf", "unknown role telepathy"):
            self.assertIn(msg, out)


class New(Base):
    def test_new_recipe_for_this_machine_goes_to_your_recipes(self):
        m = self.machine(mac_m4)
        root = m.checkout()                                  # not a git checkout
        r = m.rack("new", "tiny", "org/Tiny-1B", root=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = m.config_path("recipes", "tiny")
        self.assertEqual(sorted(os.listdir(d)), ["mac.env", "model.env"])
        model = open(os.path.join(d, "model.env")).read()
        mac = open(os.path.join(d, "mac.env")).read()
        self.assertIn("MODEL=org/Tiny-1B\n", model)
        self.assertIn("ENGINE=llamacpp", mac)
        self.assertIn('. "$RECIPE_DIR/model.env"', mac)
        self.assertIn("#     rack fit org/Tiny-1B --mac", mac)
        self.assertNotIn("# --- 1 FIT", mac.split("\n")[0])
        c = m.rack("recipes", "check", "tiny", root=root)
        self.assertEqual(c.returncode, 0, c.stdout)          # unanswered is a warning, not an error
        self.assertIn("unanswered FILL_ME", c.stdout)
        up = m.rack("up", "tiny", "--plan", root=root)
        self.assertIn("still has FILL_ME in ARTIFACT, SERVE_ARGS: answer its questions first", up.stderr)

    def test_adding_variants(self):
        m = self.machine(dgx_spark)
        root = m.checkout()
        self.assertEqual(m.rack("new", "tiny", "org/Tiny-1B-GGUF", "--windows", root=root).returncode, 0)
        d = m.config_path("recipes", "tiny")
        self.assertIn("MODEL=org/Tiny-1B\n", open(os.path.join(d, "model.env")).read())
        self.assertIn("ARTIFACT=org/Tiny-1B-GGUF/FILL_ME.gguf\n", open(os.path.join(d, "windows.env")).read())
        r = m.rack("new", "tiny", root=root)                 # this machine: dgx
        self.assertEqual(r.returncode, 0, r.stderr)
        dgx = open(os.path.join(d, "dgx.env")).read()
        self.assertIn("ENGINE=vllm", dgx)
        self.assertIn("#     rack fit org/Tiny-1B --dgx", dgx)
        self.assertNotIn("--host", dgx.split("SERVE_ARGS=(")[1].split(")")[0].replace("# rack up sets --host", ""))
        r = m.rack("new", "tiny", "--dgx", root=root)
        self.assertIn("already exists", r.stderr)
        d2 = rack_json(m.rack("recipes", "--json", root=root))
        self.assertEqual({x["name"]: x["platforms"] for x in d2["recipes"]}["tiny"], ["dgx", "windows"])

    def test_refusals(self):
        m = self.machine(dgx_spark)
        root = m.checkout()
        for args, msg in [(("new", "x"), "needs its model"), (("new", "x", "nomodel"), "org/name"),
                          (("new", "TEMPLATE-x", "a/b"), "TEMPLATE names the scaffolds"),
                          (("new", "a/b", "c/d"), "recipe names"), (("new",), "usage")]:
            r = m.rack(*args, root=root)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(msg, r.stderr, args)

    def test_a_flat_recipe_becomes_a_folder_with_its_history(self):
        m = self.machine(linux_4090x2)
        root = m.checkout(git=True)
        r = m.rack("new", "phase2-gpt-oss-120b-solo", "--mac", root=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = os.path.join(root, "recipes", "phase2-gpt-oss-120b-solo")
        self.assertEqual(sorted(os.listdir(d)), ["dgx.env", "linux.env", "mac.env", "model.env"])
        self.assertIn("MODEL=openai/gpt-oss-120b", open(os.path.join(d, "model.env")).read())
        st = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("R  recipes/phase2-gpt-oss-120b-solo.env -> recipes/phase2-gpt-oss-120b-solo/dgx.env", st)
        # it still serves on Linux, from the same flags
        p = rack_json(m.rack("up", "phase2-gpt-oss-120b-solo", "--plan", "--json", root=root))
        self.assertEqual((p["platform"], p["model"], len(p["nodes"])), ("linux", "openai/gpt-oss-120b", 1))
        self.assertTrue(p["variant"].endswith("/linux.env"))
        c = m.rack("recipes", "check", "phase2-gpt-oss-120b-solo", root=root)
        self.assertIn("0 errors", c.stdout)


if __name__ == "__main__":
    unittest.main()
