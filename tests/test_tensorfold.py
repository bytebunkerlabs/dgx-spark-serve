"""TensorFold, a third engine on a Mac: its plan, launch arguments, key
handling, recipe checks, the pull, and the Python its venv needs."""
import os
import plistlib
import subprocess
import sys
import unittest

from helpers import ROOT, dgx_spark, mac_m4
from test_plan import Base as PlanBase, after, steps

sys.path.insert(0, os.path.join(ROOT, "py"))
import engines  # noqa: E402
import recipes  # noqa: E402

REV = "f84b054c5677b6e59bdb969ae61915b0983d68bb"
TF = {"model.env": 'MODEL=Qwen/Qwen3.6-35B-A3B\nROLES="chat"\n',
      "mac.env": 'ENGINE=tensorfold\n. "$RECIPE_DIR/model.env"\nARTIFACT=TensorFold/Qwen3.6-35B-A3B-MLX-4bit-MTP\n'
                 'ARTIFACT_REVISION=%s\nSERVE_ARGS=(--context 65536 --parallel 4)\n' % REV}


def job(plan):
    (w,) = [s for s in steps(plan, "write") if s["path"].endswith(".plist")]
    return plistlib.loads(w["content"].encode())


class Plans(PlanBase):
    def mine(self, m, name, files):
        d = m.config_path("recipes", name)
        os.makedirs(d, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)

    def test_a_mac_runs_it_from_its_own_venv_under_launchd(self):
        m = self.machine(mac_m4, name="mini")
        self.mine(m, "tf", TF)
        p = self.plan(m, "tf")
        self.assertEqual((p["engine"], p["runtime"], p["host"], p["port"], p["key_required"]),
                         ("tensorfold", "launchd", "0.0.0.0", 8888, True))
        self.assertEqual([s["kind"] for s in p["steps"]],
                         ["free", "venv", "weights", "run", "write", "run", "run", "run", "wait", "record"])
        (venv,) = steps(p, "venv")
        self.assertTrue(venv["dest"].endswith("/engines/tensorfold/" + engines.TENSORFOLD_COMMIT))
        self.assertEqual((venv["package"], venv["python"]),
                         ("tensorfold @ git+https://github.com/ashhart/TensorFold.git@" + engines.TENSORFOLD_COMMIT, "3.11"))
        (w,) = steps(p, "weights")
        self.assertEqual((w["repo"], w["revision"]), ("TensorFold/Qwen3.6-35B-A3B-MLX-4bit-MTP", REV))
        j = job(p)
        a, env = j["ProgramArguments"], j["EnvironmentVariables"]
        self.assertEqual(a[:2], [venv["check"], "serve"])
        self.assertTrue(a[2].endswith("/hub/models--TensorFold--Qwen3.6-35B-A3B-MLX-4bit-MTP/snapshots/" + REV))
        self.assertEqual((after(a, "--host"), after(a, "--port"), after(a, "--name"), after(a, "--context")),
                         (["0.0.0.0"], ["8888"], ["tf"], ["65536"]))
        self.assertEqual(after(a, "--api-key-file"), [m.config_path("engine.key")])   # its own key auth, the same file
        self.assertIn("--no-update-check", a)
        self.assertEqual((env["HF_HUB_OFFLINE"], env["TENSORFOLD_NO_UPDATE_CHECK"]), ("1", "1"))
        self.assertEqual((j["Label"], j["KeepAlive"]), ("ai.bytebunker.dgx-serve.engine", {"SuccessfulExit": False}))
        self.assertEqual(p["serving"]["speculative"], {"method": "mtp", "tokens": None})
        self.assertTrue(p["serving"]["engine_build"].startswith("TensorFold 0.6.6"))

    def test_drafts_off_is_one_flag(self):
        m = self.machine(mac_m4, name="mini")
        off = dict(TF, **{"mac.env": TF["mac.env"].replace("--parallel 4)", "--parallel 4 --no-drafts)")})
        self.mine(m, "tf-off", off)
        p = self.plan(m, "tf-off")
        self.assertIn("--no-drafts", job(p)["ProgramArguments"])
        self.assertIsNone(p["serving"]["speculative"])

    def test_without_a_key_it_listens_on_loopback_only(self):
        m = self.machine(mac_m4, name="mini")
        self.mine(m, "tf", TF)
        p = self.plan(m, "tf", env={"ENGINE_KEY": "off"})
        a = job(p)["ProgramArguments"]
        self.assertEqual((p["host"], after(a, "--host"), after(a, "--api-key-file")), ("127.0.0.1", ["127.0.0.1"], []))

    def test_the_repos_recipes_plan_on_a_mac(self):
        m = self.machine(mac_m4, name="mini")
        for name, engine in (("qwen36-a3b", "llamacpp"), ("qwen36-a3b-tf", "tensorfold"),
                             ("qwen36-a3b-tf-nodraft", "tensorfold"), ("qwen3.8-27b-tf", "tensorfold"),
                             ("nemotron-3.5-lightning-tf", "tensorfold"), ("gemma-4-26b-a4b-tf", "tensorfold")):
            self.assertEqual(self.plan(m, name)["engine"], engine)

    def test_a_draft_model_is_pulled_and_passed(self):
        m = self.machine(mac_m4, name="mini")
        drev = "50307d4c4cde6860d4eee73e2547cd786fe8e8a4"
        self.mine(m, "tfd", dict(TF, **{"mac.env": TF["mac.env"] + "DRAFTER=z-lab/Qwen3.8-27B-DFlash2\n"
                                                                 "DRAFTER_REVISION=%s\n" % drev}))
        p = self.plan(m, "tfd")
        ws = steps(p, "weights")
        self.assertEqual([(w["repo"], w["revision"]) for w in ws],
                         [("TensorFold/Qwen3.6-35B-A3B-MLX-4bit-MTP", REV), ("z-lab/Qwen3.8-27B-DFlash2", drev)])
        self.assertTrue(after(job(p)["ProgramArguments"], "--drafter")[0].endswith(
            "/hub/models--z-lab--Qwen3.8-27B-DFlash2/snapshots/" + drev))
        self.assertEqual(p["serving"]["speculative"], {"method": "dflash", "model": "z-lab/Qwen3.8-27B-DFlash2"})

    def test_only_on_a_mac(self):
        m = self.machine(dgx_spark)
        self.mine(m, "tf", dict(TF, **{"dgx.env": TF["mac.env"]}))
        r = m.rack("up", "tf", "--plan")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TensorFold serves on a Mac", r.stdout + r.stderr)


class Checks(PlanBase):
    def mine(self, m, name, files):
        Plans.mine(self, m, name, files)

    def test_check_accepts_it_and_names_what_is_wrong(self):
        m = self.machine(mac_m4)
        self.mine(m, "tf", TF)
        self.assertEqual(m.rack("recipes", "check", "tf").returncode, 0)
        self.mine(m, "bad", {"model.env": TF["model.env"],
                             "mac.env": 'ENGINE=tensorfold\n. "$RECIPE_DIR/model.env"\n'
                                        'ARTIFACT=TensorFold/Qwen3.6-35B-A3B-MLX-4bit-MTP/x.gguf\nARTIFACT_REVISION=main\n'
                                        'SERVE_ARGS=(--name mine --no-update-check)\n',
                             "linux.env": 'ENGINE=tensorfold\n. "$RECIPE_DIR/model.env"\nARTIFACT=a/b\n'})
        out = m.rack("recipes", "check", "bad").stdout
        for msg in ("TensorFold needs ARTIFACT=<org>/<repo>", "TensorFold needs ARTIFACT_REVISION",
                    "--name is the launcher's", "--no-update-check is the launcher's",
                    "TensorFold serves on a Mac here"):
            self.assertIn(msg, out)

    def test_a_draft_model_needs_its_commit_and_is_tensorfolds(self):
        m = self.machine(mac_m4)
        self.mine(m, "d1", {"model.env": TF["model.env"],
                            "mac.env": TF["mac.env"].replace("--parallel 4)", "--parallel 4 --drafter x/y)")
                                       + "DRAFTER=z-lab/Qwen3.8-27B-DFlash2\n"})
        self.mine(m, "d2", {"model.env": TF["model.env"],
                            "mac.env": 'ENGINE=llamacpp\n. "$RECIPE_DIR/model.env"\nARTIFACT=a/b/c.gguf\nDRAFTER=a/b\n'})
        out = m.rack("recipes", "check", "d1").stdout + m.rack("recipes", "check", "d2").stdout
        for msg in ("DRAFTER needs DRAFTER_REVISION", "--drafter is the launcher's", "DRAFTER is TensorFold's"):
            self.assertIn(msg, out)

    def test_the_repos_draft_model_recipe_pulls_both(self):
        d = os.path.join(ROOT, "recipes", "qwen3.8-27b-tf")
        spec = recipes.pull_spec(os.path.join(d, "mac.env"), d, ROOT, "mac")
        self.assertEqual([p["repo"] for p in spec["pulls"]], ["TensorFold/Qwen3.8-27B-MLX-4bit", "z-lab/Qwen3.8-27B-DFlash2"])

    def test_pull_fetches_the_checkpoint_at_its_commit(self):
        d = os.path.join(ROOT, "recipes", "qwen36-a3b-tf")
        spec = recipes.pull_spec(os.path.join(d, "mac.env"), d, ROOT, "mac")
        self.assertEqual(spec["pulls"], [{"repo": "TensorFold/Qwen3.6-35B-A3B-MLX-4bit-MTP", "revision": REV,
                                          "weights": True}])


class Python(unittest.TestCase):
    """The venv needs Python 3.11+: one on PATH, else one uv has or installs."""

    def fake(self, versions, uv=None):
        calls = []

        def which(name):
            return "/bin/" + name if name in versions or (name == "uv" and uv is not None) else None

        def run(argv, **kw):
            calls.append(argv)
            if argv[0] == "/bin/uv":
                if argv[1:3] == ["python", "find"]:
                    found = uv.get("have")
                    return subprocess.CompletedProcess(argv, 0 if found else 2, (found or "") + "\n", "")
                uv["have"] = "/uv/py3.12"
                return subprocess.CompletedProcess(argv, 0, "", "")
            v = versions.get(os.path.basename(argv[0]), "3.12") if argv[0].startswith("/bin/") else "3.12"
            return subprocess.CompletedProcess(argv, 0, v + "\n", "")
        return which, run, calls

    def test_on_path(self):
        which, run, _ = self.fake({"python3.12": "3.12"})
        self.assertEqual(engines.python_at_least((3, 11), which, run), "/bin/python3.12")

    def test_from_uv_installing_it_when_needed(self):
        which, run, calls = self.fake({}, uv={})
        self.assertEqual(engines.python_at_least((3, 11), which, run), "/uv/py3.12")
        self.assertIn(["/bin/uv", "python", "install", "3.12"], calls)

    def test_none(self):
        which, run, _ = self.fake({})
        self.assertIsNone(engines.python_at_least((3, 11), which, run))


if __name__ == "__main__":
    unittest.main()
