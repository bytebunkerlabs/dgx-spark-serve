"""Shared test harness: run rack's bash with a faked machine.

A FakeMachine is a temporary directory with
  sysroot/   files the probes read (RACK_SYSROOT): /proc, /sys, /etc
  fakebin/   executables that stand in for uname, nvidia-smi, sysctl, docker...
  sysbin/    links to the few real tools the scripts need (awk, sed, python3...)
  home/      HOME, with dgx-serve's config and state under it
PATH is fakebin:sysbin only, so a real nvidia-smi or docker on the test host
can never leak into a fixture. Scripts run under /bin/bash (3.2 on macOS).
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"
TOOLS = ["awk", "sed", "tr", "cut", "head", "tail", "cat", "grep", "sort", "uniq", "wc", "date",
         "mkdir", "mv", "rm", "cp", "dirname", "basename", "ls", "find", "env", "readlink", "chmod",
         "ln", "mktemp", "touch", "sleep", "seq", "id", "python3", "bash", "sh", "tee", "printf",
         "true", "false", "test", "expr", "od", "xargs", "comm", "diff", "stat", "hostname"]


class FakeMachine:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="rack-test-")
        self.sysroot = os.path.join(self.dir, "sysroot")
        self.fakebin = os.path.join(self.dir, "fakebin")
        self.sysbin = os.path.join(self.dir, "sysbin")
        self.home = os.path.join(self.dir, "home")
        for d in (self.sysroot, self.fakebin, self.sysbin, self.home):
            os.makedirs(d)
        for t in TOOLS:
            real = shutil.which(t, path="/usr/bin:/bin:/usr/sbin:/sbin")
            if real:
                os.symlink(real, os.path.join(self.sysbin, t))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def file(self, path, content):
        p = self.sysroot + path
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def mkdir(self, path):
        os.makedirs(self.sysroot + path, exist_ok=True)

    def cmd(self, name, script):
        """An executable stand-in; `script` is a sh body ($@ are the args)."""
        p = os.path.join(self.fakebin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def env(self, extra=None):
        e = {
            "PATH": self.fakebin + ":" + self.sysbin,
            "HOME": self.home,
            "RACK_SYSROOT": self.sysroot,
            "DGX_SERVE_CONFIG": os.path.join(self.home, ".config", "dgx-serve"),
            "DGX_SERVE_STATE": os.path.join(self.home, ".local", "state", "dgx-serve"),
            "LANG": "C",
            "TERM": "dumb",
        }
        if extra:
            e.update(extra)
        return e

    def bash(self, snippet, extra_env=None, cwd=ROOT):
        """Source the libraries and run a snippet; returns CompletedProcess."""
        prelude = ". lib/common.sh; . lib/platform.sh; "
        for lib in ("inventory", "flags", "recipe"):
            if os.path.exists(os.path.join(ROOT, "lib", lib + ".sh")):
                prelude += ". lib/%s.sh; " % lib
        return subprocess.run([BASH, "-c", prelude + snippet], cwd=cwd, env=self.env(extra_env),
                              capture_output=True, text=True, timeout=60)

    def rack(self, *args, extra_env=None, cwd=ROOT):
        return subprocess.run([BASH, os.path.join(ROOT, "rack")] + list(args), cwd=cwd,
                              env=self.env(extra_env), capture_output=True, text=True, timeout=120)


def rack_json(proc):
    if proc.returncode != 0:
        raise AssertionError("rack failed (%d): %s%s" % (proc.returncode, proc.stdout, proc.stderr))
    return json.loads(proc.stdout)


# --------------------------------------------------------------- machines --
def uname_cmd(m, s, arch, release):
    m.cmd("uname", 'case "$1" in -s) echo %s;; -m) echo %s;; -r) echo %s;; *) echo %s;; esac' % (s, arch, release, s))


def dgx_spark(m):
    uname_cmd(m, "Linux", "aarch64", "6.17.0-1026-nvidia")
    m.file("/proc/version", "Linux version 6.17.0-1026-nvidia (buildd@ubuntu) #26-Ubuntu SMP\n")
    m.file("/sys/class/dmi/id/product_name", "NVIDIA_DGX_Spark\n")
    m.file("/sys/class/dmi/id/product_family", "DGX Spark\n")
    m.file("/proc/meminfo", "MemTotal:       127600812 kB\nMemFree:        1000 kB\n")
    m.file("/etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.4 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\n')
    m.file("/proc/cpuinfo", "processor\t: 0\nCPU part\t: 0xd85\n")
    m.mkdir("/run/systemd/system")
    m.cmd("nvidia-smi", 'case "$*" in *--query-gpu*) echo "NVIDIA GB10, [N/A], 12.1, 580.159.03";; '
                        '*) echo "| NVIDIA-SMI 580.159.03   Driver Version: 580.159.03   CUDA Version: 13.0 |";; esac')
    m.cmd("docker", 'case "$*" in "info --format"*) echo \'{"io.containerd.runc.v2":{},"nvidia":{}}\';; info) exit 0;; esac')


def linux_4090x2(m):
    uname_cmd(m, "Linux", "x86_64", "6.8.0-45-generic")
    m.file("/proc/version", "Linux version 6.8.0-45-generic\n")
    m.file("/sys/class/dmi/id/product_name", "X870E AORUS PRO\n")
    m.file("/proc/meminfo", "MemTotal:       131072000 kB\n")
    m.file("/etc/os-release", 'PRETTY_NAME="Ubuntu 22.04.5 LTS"\nVERSION_ID="22.04"\n')
    m.file("/proc/cpuinfo", "model name\t: AMD Ryzen 9 9950X 16-Core Processor\n")
    m.mkdir("/run/systemd/system")
    m.cmd("nvidia-smi", 'case "$*" in *--query-gpu*) printf "NVIDIA GeForce RTX 4090, 24564, 8.9, 575.57\\n'
                        'NVIDIA GeForce RTX 4090, 24564, 8.9, 575.57\\n";; *) echo "CUDA Version: 12.9";; esac')
    m.cmd("docker", 'case "$*" in "info --format"*) echo \'{"runc":{}}\';; info) exit 0;; esac')
    m.cmd("nvidia-ctk", "exit 0")


def wsl_2070(m):
    uname_cmd(m, "Linux", "x86_64", "5.15.167.4-microsoft-standard-WSL2")
    m.file("/proc/version", "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@...) #1 SMP\n")
    m.file("/proc/meminfo", "MemTotal:       16303264 kB\n")
    m.file("/etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nVERSION_ID="24.04"\n')
    m.mkdir("/run/systemd/system")
    m.cmd("nvidia-smi", 'case "$*" in *--query-gpu*) echo "NVIDIA GeForce RTX 2070, 8192, 7.5, 576.88";; '
                        '*) echo "CUDA Version: 12.9";; esac')
    m.cmd("cmd.exe", 'printf "\\r\\nMicrosoft Windows [Version 10.0.26100.4652]\\r\\n"')


def mac_m4(m, memsize=17179869184, wired=0, version="27.0.1", arch="arm64", cpu="Apple M4"):
    uname_cmd(m, "Darwin", arch, "26.0.0")
    m.cmd("sysctl", 'case "$2" in hw.memsize) echo %d;; iogpu.wired_limit_mb) echo %d;; '
                    'machdep.cpu.brand_string) echo "%s";; *) exit 1;; esac' % (memsize, wired, cpu))
    m.cmd("sw_vers", 'echo %s' % version)


def linux_no_gpu(m):
    uname_cmd(m, "Linux", "x86_64", "6.8.0-45-generic")
    m.file("/proc/version", "Linux version 6.8.0-45-generic\n")
    m.file("/proc/meminfo", "MemTotal:       32768000 kB\n")
    m.file("/etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nVERSION_ID="24.04"\n')
