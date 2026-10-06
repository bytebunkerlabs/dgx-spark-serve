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
TOOLS = ["awk", "sed", "tr", "cut", "head", "tail", "cat", "grep", "sort", "uniq", "wc", "date", "tar",
         "mkdir", "mv", "rm", "cp", "dirname", "basename", "ls", "find", "env", "readlink", "chmod",
         "ln", "mktemp", "touch", "sleep", "seq", "id", "python3", "bash", "sh", "tee", "printf",
         "true", "false", "test", "expr", "od", "xargs", "comm", "diff", "stat", "df", "git"]


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

    def hostname(self, name):
        self.cmd("hostname", "echo %s" % name)

    def ips(self, *pairs):
        """IPv4 addresses this machine owns, as (interface, address) pairs."""
        lines = "".join("%d: %s    inet %s/24 brd 0.0.0.0 scope global %s\\n" % (i + 2, ifc, ip, ifc)
                        for i, (ifc, ip) in enumerate(pairs))
        br = "".join('    %s) echo "%s UP %s/24";;\n' % (ifc, ifc, ip) for ifc, ip in pairs)
        # `ip route get X` leaves from the interface on X's /24
        rt = "".join('    %s.*) echo "$last dev %s src %s uid 501";;\n' % (ip.rsplit(".", 1)[0], ifc, ip)
                     for ifc, ip in pairs)
        self.cmd("ip", 'eval last=\\${$#}\n'
                       'case "$*" in\n'
                       '  *-br*) case "$5" in\n%s    esac;;\n'
                       '  *route*) case "$last" in\n%s    *) exit 2;;\n    esac;;\n'
                       '  *addr*) printf "%s";;\n'
                       'esac' % (br, rt, lines))

    def dotenv(self, text):
        """The checkout's .env, as rack reads it."""
        with open(os.path.join(self.home, "checkout.env"), "w") as f:
            f.write(text)

    def config_path(self, *parts):
        return os.path.join(self.home, ".config", "dgx-serve", *parts)

    def log(self, name):
        """Calls a logging stand-in recorded (one line each), or []."""
        p = os.path.join(self.home, name + ".log")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def logging_cmd(self, name, body="exit 0"):
        """A stand-in that records its arguments in home/<name>.log, then runs body."""
        self.cmd(name, 'printf "%%s\\n" "$*" >> "$HOME/%s.log"\n%s' % (name, body))

    def log_calls(self, name):
        """Make an existing stand-in record its arguments too (or add one that does)."""
        p = os.path.join(self.fakebin, name)
        body = open(p).read().split("\n", 1)[1] if os.path.exists(p) else "exit 0"
        self.logging_cmd(name, body)

    def env(self, extra=None):
        e = {
            "PATH": self.fakebin + ":" + self.sysbin,
            "HOME": self.home,
            "RACK_SYSROOT": self.sysroot,
            "DGX_SERVE_CONFIG": os.path.join(self.home, ".config", "dgx-serve"),
            "DGX_SERVE_STATE": os.path.join(self.home, ".local", "state", "dgx-serve"),
            # the checkout's .env, if a test writes one (never the repo's own)
            "DGX_SERVE_DOTENV": os.path.join(self.home, "checkout.env"),
            # nothing a test runs may reach the internet: the hub is a dead
            # port unless a test brings its own, and downloads stay local
            "HF_ENDPOINT": "http://127.0.0.1:9",
            "RACK_OFFLINE": "1",
            "LANG": "C",
            "TERM": "dumb",
        }
        if extra:
            e.update(extra)
        return e

    def bash(self, snippet, extra_env=None, cwd=ROOT):
        """Source the libraries and run a snippet; returns CompletedProcess."""
        prelude = ". lib/common.sh; . lib/platform.sh; "
        for lib in ("inventory", "nodes", "flags", "recipe"):
            if os.path.exists(os.path.join(ROOT, "lib", lib + ".sh")):
                prelude += ". lib/%s.sh; " % lib
        return subprocess.run([BASH, "-c", prelude + snippet], cwd=cwd, env=self.env(extra_env),
                              capture_output=True, text=True, timeout=60)

    def rack(self, *args, extra_env=None, cwd=ROOT, root=ROOT):
        return subprocess.run([BASH, os.path.join(root, "rack")] + list(args), cwd=cwd,
                              env=self.env(extra_env), capture_output=True, text=True, timeout=120)

    def checkout(self, git=False):
        """A private copy of the checkout, for commands that write into it."""
        dst = os.path.join(self.dir, "checkout")
        for part in ("rack", "lib", "py", "scripts", "recipes", "monitor"):
            src = os.path.join(ROOT, part)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dst, part), ignore=shutil.ignore_patterns("__pycache__"))
            else:
                os.makedirs(dst, exist_ok=True)
                shutil.copy2(src, os.path.join(dst, part))
        if git:
            g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", dst]
            for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
                subprocess.run(g + cmd, check=True, capture_output=True)
        return dst


SSH_FAKE = r'''
printf '%%s\n' "$*" >> "$HOME/ssh.log"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-c|-i|-p|-l|-F|-J) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host=$1; shift
m="%s/$host"
[ -d "$m" ] || { echo "ssh: Could not resolve hostname $host" >&2; exit 255; }
[ -f "$m/down" ] && { echo "ssh: connect to host $host port 22: Operation timed out" >&2; exit 255; }
export PATH="$m/fakebin:$m/sysbin" RACK_SYSROOT="$m/sysroot" HOME="$m/home"
export DGX_SERVE_CONFIG="$m/home/.config/dgx-serve" DGX_SERVE_STATE="$m/home/.local/state/dgx-serve"
export DGX_SERVE_DOTENV="$m/home/checkout.env"
cd "$HOME" || exit 255
[ $# -eq 0 ] && exit 0
exec bash -c "$*"
'''


class FakeNet:
    """Machines that reach each other over a fake ssh: `ssh <host> cmd` runs
    cmd as that machine (its PATH, sysroot and home), from its home."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="rack-net-")
        self.machines = []

    def add(self, machine, *hosts):
        for h in hosts:
            os.symlink(machine.dir, os.path.join(self.dir, h))
        machine.cmd("ssh", SSH_FAKE % self.dir)
        self.machines.append(machine)
        return machine

    def down(self, machine):
        open(os.path.join(machine.dir, "down"), "w").close()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


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
