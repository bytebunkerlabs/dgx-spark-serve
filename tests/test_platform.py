"""lib/platform.sh: every machine dgx-serve meets, faked."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import FakeMachine, rack_json, dgx_spark, linux_4090x2, wsl_2070, mac_m4, linux_no_gpu  # noqa: E402


class Platform(unittest.TestCase):
    def setUp(self):
        self.m = FakeMachine()

    def tearDown(self):
        self.m.cleanup()

    def detect(self):
        return rack_json(self.m.rack("platform", "--json"))

    def test_dgx_spark(self):
        dgx_spark(self.m)
        p = self.detect()
        self.assertEqual(p["platform"], "dgx")
        self.assertTrue(p["supported"] and p["unified_memory"] and p["docker"] and p["nvidia_container_runtime"])
        self.assertEqual(p["gpus"], [{"name": "NVIDIA GB10", "memory_mb": 0, "compute_capability": "12.1"}])
        self.assertEqual(p["model_budget_mb"], 124610)          # unified: system memory
        self.assertEqual((p["cuda"], p["init"]), ("13.0", "systemd"))
        self.assertIn("DGX Spark", p["description"])

    def test_linux_two_gpus(self):
        linux_4090x2(self.m)
        p = self.detect()
        self.assertEqual(p["platform"], "linux")
        self.assertEqual(len(p["gpus"]), 2)
        self.assertEqual(p["model_budget_mb"], 49128)
        self.assertTrue(p["nvidia_container_runtime"])          # nvidia-ctk, though docker lists no runtime
        self.assertTrue(p["supported"])
        self.assertEqual(p["cpu"], "AMD Ryzen 9 9950X 16-Core Processor")

    def test_wsl2(self):
        wsl_2070(self.m)
        p = self.detect()
        self.assertEqual(p["platform"], "windows")
        self.assertTrue(p["wsl"] and p["supported"])
        self.assertEqual(p["windows_build"], 26100)
        self.assertEqual(p["gpus"][0]["compute_capability"], "7.5")

    def test_wsl2_on_windows_10_is_refused(self):
        wsl_2070(self.m)
        self.m.cmd("cmd.exe", 'printf "Microsoft Windows [Version 10.0.19045.5131]\\r\\n"')
        p = self.detect()
        self.assertFalse(p["supported"])
        self.assertIn("Windows 11", p["support_note"])

    def test_wsl2_without_gpu(self):
        wsl_2070(self.m)
        os.remove(os.path.join(self.m.fakebin, "nvidia-smi"))
        p = self.detect()
        self.assertEqual(p["platform"], "unsupported")
        self.assertIn("driver on Windows", p["reason"])

    def test_mac_16gb(self):
        mac_m4(self.m)
        p = self.detect()
        self.assertEqual(p["platform"], "mac")
        self.assertEqual(p["metal_budget_mb"], 10922)           # two thirds of 16 GiB
        self.assertTrue(p["supported"] and p["unified_memory"])
        self.assertIn("Apple M4", p["description"])

    def test_mac_large_and_raised_limit(self):
        mac_m4(self.m, memsize=128 * 1024 ** 3)
        self.assertEqual(self.detect()["metal_budget_mb"], 98304)  # three quarters above 36 GB
        self.m.cleanup()
        self.m = FakeMachine()
        mac_m4(self.m, memsize=64 * 1024 ** 3, wired=57344)
        self.assertEqual(self.detect()["metal_budget_mb"], 57344)  # iogpu.wired_limit_mb wins

    def test_old_macos_and_intel_mac(self):
        mac_m4(self.m, version="13.6")
        p = self.detect()
        self.assertFalse(p["supported"])
        self.assertIn("macOS 14", p["support_note"])
        self.m.cleanup()
        self.m = FakeMachine()
        mac_m4(self.m, arch="x86_64", cpu="Intel(R) Core(TM) i9")
        p = self.detect()
        self.assertEqual(p["platform"], "unsupported")
        self.assertIn("Apple Silicon", p["reason"])

    def test_linux_without_gpu(self):
        linux_no_gpu(self.m)
        p = self.detect()
        self.assertEqual(p["platform"], "unsupported")
        self.assertIn("no NVIDIA GPU", p["reason"])

    def test_human_output(self):
        dgx_spark(self.m)
        r = self.m.rack("platform")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DGX Spark", r.stdout)


if __name__ == "__main__":
    unittest.main()
