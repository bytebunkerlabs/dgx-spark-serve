#!/usr/bin/env python3
"""rack monitor: read-only telemetry for every node in the rack.

One file, standard library only, two modes:

  rackmon.py serve          sample this node every MONITOR_SAMPLE_S seconds and
                            answer GET /v1/node and GET /v1/cluster (a head
                            merges its peers). One token, Bearer or Basic auth.
  rackmon.py docker-relay   offline helper that holds the Docker socket and
                            writes the container list to a file. The
                            network-facing process never touches Docker.
  rackmon.py once           print one snapshot of this node and exit (debug).

`rack monitor up` builds the image and runs both on every node. Nothing here
writes to the host, and everything reported is measured on the node it
describes: a field this node cannot measure is null, never a guess.
"""
import base64
import collections
import csv
import hmac
import http.client
import io
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
SCHEMA = 1
HISTORY_MAX = 450          # samples kept per node: 15 min at the default 2 s step


def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


# ===================================================================== parse ==
# Pure functions over the text the kernel, nvidia-smi and the engines emit.
# test_rackmon.py pins each one against captured output.

def parse_proc_stat(text):
    """{'cpu': (idle, total), 'cpu0': (idle, total), ...} in jiffies.
    guest and guest_nice are already counted inside user and nice."""
    out = {}
    for ln in text.splitlines():
        if not ln.startswith("cpu"):
            continue
        parts = ln.split()
        vals = [int(x) for x in parts[1:9]]
        vals += [0] * (8 - len(vals))
        out[parts[0]] = (vals[3] + vals[4], sum(vals))     # idle + iowait
    return out


def busy_pct(prev, cur):
    if not prev or not cur:
        return None
    d_total = cur[1] - prev[1]
    if d_total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - (cur[0] - prev[0]) / d_total))), 1)


def parse_meminfo(text):
    """Field -> bytes (kB lines scaled; bare counts left as is)."""
    out = {}
    for ln in text.splitlines():
        key, _, rest = ln.partition(":")
        f = rest.split()
        if f and f[0].isdigit():
            out[key.strip()] = int(f[0]) * (1024 if len(f) > 1 and f[1] == "kB" else 1)
    return out


def parse_net_dev(text):
    """iface -> (rx_bytes, tx_bytes)."""
    out = {}
    for ln in text.splitlines()[2:]:
        name, _, rest = ln.partition(":")
        f = rest.split()
        if len(f) >= 9:
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def parse_loadavg(text):
    try:
        return [float(x) for x in text.split()[:3]]
    except ValueError:
        return None


_NA = {"", "n/a", "[n/a]", "na", "not supported", "[not supported]", "unknown",
       "[unknown error]", "[insufficient permissions]", "[gpu is lost]", "[gpu requires reset]"}


def num(v):
    """nvidia-smi / sysfs scalar -> int or float; every flavour of N/A -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _NA:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f) if f.is_integer() else f


def parse_csv(text, fields):
    """nvidia-smi --format=csv,noheader,nounits -> list of dicts keyed by field."""
    rows = []
    for rec in csv.reader(io.StringIO(text.strip()), skipinitialspace=True):
        if not rec or not any(x.strip() for x in rec):
            continue
        rows.append({k: (rec[i].strip() if i < len(rec) else "") for i, k in enumerate(fields)})
    return rows


# clocks_event_reasons bit -> name. Idle, app/display clock settings and sync
# boost are the GPU doing as told; the UI only raises the others.
THROTTLE_BITS = ((0x2, "app clocks"), (0x4, "power cap"), (0x8, "hw slowdown"),
                 (0x10, "sync boost"), (0x20, "thermal"), (0x40, "hw thermal"),
                 (0x80, "power brake"), (0x100, "display clocks"))
THROTTLE_BAD = {"power cap", "hw slowdown", "thermal", "hw thermal", "power brake"}


def decode_throttle(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s.startswith("0x"):
        return None
    try:
        mask = int(s, 16)
    except ValueError:
        return None
    return [name for bit, name in THROTTLE_BITS if mask & bit]


_PROM = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prom(text):
    """Prometheus text exposition -> [(name, {labels}, value)]; NaN dropped."""
    out = []
    for ln in text.splitlines():
        if not ln or ln[0] == "#":
            continue
        m = _PROM.match(ln)
        if not m:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if math.isnan(v):
            continue
        out.append((m.group(1), dict(_LABEL.findall(m.group(2) or "")), v))
    return out


# Engine metric families. Totals are summed across label sets (models, data
# parallel ranks); KV usage takes the fullest rank, which is the one that
# will preempt first.
ENGINES = {
    "vllm": {
        "prefix": "vllm:",
        "running": ("vllm:num_requests_running",),
        "waiting": ("vllm:num_requests_waiting",),
        "kv": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
        "gen": ("vllm:generation_tokens_total",),
        "prompt": ("vllm:prompt_tokens_total",),
        "ok": ("vllm:request_success_total",),
        "preempt": ("vllm:num_preemptions_total",),
        "hits": ("vllm:prefix_cache_hits_total",),
        "queries": ("vllm:prefix_cache_queries_total",),
        "ttft": "vllm:time_to_first_token_seconds",
        "e2e": "vllm:e2e_request_latency_seconds",
        "itl": ("vllm:inter_token_latency_seconds", "vllm:time_per_output_token_seconds"),
        # speculative decoding (MTP, EAGLE, n-gram): one decode step can emit
        # several tokens, so the step latency above is not a per-token latency
        "spec_drafts": ("vllm:spec_decode_num_drafts_total",),
        "spec_draft_tokens": ("vllm:spec_decode_num_draft_tokens_total",),
        "spec_accepted": ("vllm:spec_decode_num_accepted_tokens_total",),
    },
    "sglang": {
        "prefix": "sglang:",
        "running": ("sglang:num_running_reqs",),
        "waiting": ("sglang:num_queue_reqs",),
        "kv": ("sglang:token_usage",),
        "gen": ("sglang:generation_tokens_total",),
        "prompt": ("sglang:prompt_tokens_total",),
        "ttft": "sglang:time_to_first_token_seconds",
        "e2e": "sglang:e2e_request_latency_seconds",
        "itl": ("sglang:inter_token_latency_seconds", "sglang:time_per_output_token_seconds"),
    },
    "llama.cpp": {
        "prefix": "llamacpp:",
        "running": ("llamacpp:requests_processing",),
        "waiting": ("llamacpp:requests_deferred",),
        "kv": ("llamacpp:kv_cache_usage_ratio",),
        "gen": ("llamacpp:tokens_predicted_total",),
        "prompt": ("llamacpp:prompt_tokens_total",),
    },
}


def engine_kind(text):
    for kind, spec in ENGINES.items():
        if re.search("^" + re.escape(spec["prefix"]), text, re.M):
            return kind
    return None


def summarize_engine(kind, samples):
    """One engine's /metrics -> counters and gauges (windows are computed later
    from successive summaries; see Engine.window)."""
    spec = ENGINES[kind]
    tot = collections.defaultdict(float)
    peak = {}
    buckets = {}
    models = set()
    kv_tokens = None
    for name, labels, v in samples:
        if name.startswith(spec["prefix"]) and labels.get("model_name"):
            models.add(labels["model_name"])
        tot[name] += v
        peak[name] = max(peak.get(name, v), v)
        if name.endswith("_bucket") and "le" in labels:
            le = float("inf") if labels["le"] in ("+Inf", "Inf", "inf") else float(labels["le"])
            b = buckets.setdefault(name[:-len("_bucket")], {})
            b[le] = b.get(le, 0.0) + v
        if name == "vllm:cache_config_info":
            try:
                kv_tokens = int(float(labels["num_gpu_blocks"])) * int(float(labels.get("block_size") or 16))
            except (KeyError, ValueError):
                pass

    def first(keys, table=tot):
        for k in keys:
            if k in table:
                return table[k]
        return None

    def hist(base):
        if not base or (base + "_count") not in tot:
            return None
        return {"sum": tot.get(base + "_sum", 0.0), "count": tot[base + "_count"],
                "buckets": buckets.get(base) or {}}

    itl = None
    for base in spec.get("itl", ()):
        itl = hist(base)
        if itl:
            break
    kv = first(spec["kv"], peak)
    running, waiting = first(spec["running"]), first(spec["waiting"])
    return {
        "models": sorted(models),
        "running": int(running) if running is not None else None,
        "waiting": int(waiting) if waiting is not None else None,
        "kv_pct": round(kv * 100, 1) if kv is not None else None,
        "kv_tokens": kv_tokens,
        "gen_total": first(spec["gen"]),
        "prompt_total": first(spec["prompt"]),
        "requests_ok": first(spec.get("ok", ())),
        "preemptions": first(spec.get("preempt", ())),
        "prefix_hits": first(spec.get("hits", ())),
        "prefix_queries": first(spec.get("queries", ())),
        "spec_drafts": first(spec.get("spec_drafts", ())),
        "spec_draft_tokens": first(spec.get("spec_draft_tokens", ())),
        "spec_accepted": first(spec.get("spec_accepted", ())),
        "ttft": hist(spec.get("ttft")),
        "e2e": hist(spec.get("e2e")),
        "itl": itl,
    }


def hist_quantile(q, cur, prev=None):
    """histogram_quantile over cumulative buckets {le: count}; with `prev`,
    over the requests between the two snapshots only."""
    if not cur:
        return None
    les = sorted(cur)
    counts = [cur[le] - ((prev or {}).get(le, 0.0)) for le in les]
    total = counts[-1]
    if total <= 0:
        return None
    rank = q * total
    lo_le, lo_c = 0.0, 0.0
    for le, c in zip(les, counts):
        if c >= rank:
            if math.isinf(le):
                return lo_le
            if c == lo_c:
                return le
            return lo_le + (le - lo_le) * (rank - lo_c) / (c - lo_c)
        lo_le, lo_c = le, c
    return lo_le


def parse_listen(text, v6=False):
    """/proc/net/tcp{,6} -> [(ip, port)] of LISTEN sockets."""
    out = []
    for ln in text.splitlines()[1:]:
        f = ln.split()
        if len(f) < 4 or f[3] != "0A":
            continue
        addr, _, port = f[1].partition(":")
        try:
            raw = bytes.fromhex(addr)
            if v6:
                raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                ip = socket.inet_ntop(socket.AF_INET6, raw)
            else:
                ip = socket.inet_ntoa(raw[::-1])
            out.append((ip, int(port, 16)))
        except (ValueError, OSError):
            continue
    return out


def parse_os_release(text):
    d = {}
    for ln in text.splitlines():
        k, _, v = ln.partition("=")
        if k:
            d[k.strip()] = v.strip().strip('"')
    return d.get("PRETTY_NAME") or " ".join(x for x in (d.get("NAME"), d.get("VERSION")) if x) or None


ARM_PARTS = {
    0xd85: "Cortex-X925", 0xd87: "Cortex-A725", 0xd4f: "Neoverse V2", 0xd40: "Neoverse V1",
    0xd84: "Neoverse V3", 0xd0c: "Neoverse N1", 0xd49: "Neoverse N2", 0xd8e: "Neoverse N3",
    0xd82: "Cortex-X4", 0xd81: "Cortex-A720", 0xd80: "Cortex-A520", 0xd4e: "Cortex-X3",
    0xd4d: "Cortex-A715", 0xd48: "Cortex-X2", 0xd47: "Cortex-A710", 0xd46: "Cortex-A510",
    0xd44: "Cortex-X1", 0xd41: "Cortex-A78", 0xd0b: "Cortex-A76", 0xd08: "Cortex-A72",
    0xd05: "Cortex-A55", 0xd03: "Cortex-A53",
}


def parse_cpu_model(text):
    """x86 'model name', or the ARM core mix: '10x Cortex-X925 + 10x Cortex-A725'."""
    names = collections.Counter()
    for block in text.split("\n\n"):
        f = {}
        for ln in block.splitlines():
            k, _, v = ln.partition(":")
            f[k.strip().lower()] = v.strip()
        if "model name" in f:
            names[re.sub(r"\s+", " ", f["model name"])] += 1
        elif "cpu part" in f:
            try:
                part = int(f["cpu part"], 16)
            except ValueError:
                continue
            names[ARM_PARTS.get(part, "ARM part %#x" % part)] += 1
    if not names:
        return None
    if len(names) == 1:
        return next(iter(names))
    # most cores first; on a tie the bigger core (X over A, V over N) first
    mix = sorted(sorted(names.items(), reverse=True), key=lambda kv: -kv[1])
    return " + ".join("%dx %s" % (n, name) for name, n in mix)


def container_of(cgroup_text):
    """64-hex Docker container id from /proc/<pid>/cgroup, if any."""
    m = re.search(r"docker[-/]([0-9a-f]{64})", cgroup_text)
    return m.group(1) if m else None


def proc_label(cmdline, fallback):
    """A short, secret-free label for a GPU process. Command lines can carry
    API keys, so only the engine family or the program name leaves the node."""
    low = cmdline.lower()
    for key, label in (("vllm", "vllm"), ("sglang", "sglang"), ("llama-server", "llama.cpp"),
                       ("ollama", "ollama"), ("tritonserver", "triton"), ("comfyui", "comfyui")):
        if key in low:
            return label
    argv0 = cmdline.split("\0", 1)[0] or fallback or "?"
    return os.path.basename(argv0.strip())[:40] or "?"


# =================================================================== sampler ==
class Engine:
    """One inference server found on a local port, with enough of its own
    history to turn counters into rates and histograms into percentiles."""

    def __init__(self, host, port, kind):
        self.host, self.port, self.kind = host, port, kind
        self.hist = collections.deque(maxlen=48)     # (t, summary) ~ 96 s at 2 s
        self.models, self.max_len, self.models_at = [], None, 0.0
        self.error = None
        self.extra = {}

    def url(self, path):
        h = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d%s" % (h, self.port, path)

    def poll(self, now):
        try:
            if self.kind == "ollama":
                ps = http_json(self.url("/api/ps"), timeout=1.5)
                ms = ps.get("models") or []
                self.models = [m.get("name") for m in ms if m.get("name")]
                self.extra = {"loaded_bytes": sum(int(m.get("size_vram") or m.get("size") or 0) for m in ms)}
                self.error = None
                return
            if self.kind == "openai":
                self.refresh_models(now)
                self.error = None
                return
            text = http_text(self.url("/metrics"), timeout=1.5, limit=8 << 20)
            self.hist.append((now, summarize_engine(self.kind, parse_prom(text))))
            self.refresh_models(now)
            self.error = None
        except Exception as e:  # noqa: BLE001 - an engine going away is normal
            self.error = str(e)[:160]

    def refresh_models(self, now):
        if now - self.models_at < 30:
            return
        self.models_at = now
        try:
            d = http_json(self.url("/v1/models"), timeout=1.5)
            data = d.get("data") or []
            self.models = [m.get("id") for m in data if m.get("id")]
            lens = [m.get("max_model_len") for m in data if isinstance(m.get("max_model_len"), int)]
            self.max_len = max(lens) if lens else None
        except Exception:  # noqa: BLE001 - /v1/models may sit behind --api-key
            pass

    def _since(self, seconds):
        if len(self.hist) < 2:
            return None, None
        t1, cur = self.hist[-1]
        old = None
        for t0, s in self.hist:
            if t1 - t0 <= seconds + 0.5:
                old = (t0, s)
                break
        if old is None or old[0] >= t1:
            return None, None
        return old, (t1, cur)

    def window(self):
        """Rates over ~10 s, latencies and prefix hits over ~60 s."""
        out = {"gen_tps": None, "prompt_tps": None, "ttft_p50": None, "ttft_p95": None,
               "e2e_avg": None, "itl_ms": None, "prefix_hit_pct": None,
               "tokens_per_step": None, "spec_accept_pct": None, "spec_window": None}
        old, cur = self._since(10)
        if old:
            dt = cur[0] - old[0]
            for key, field in (("gen_tps", "gen_total"), ("prompt_tps", "prompt_total")):
                a, b = old[1].get(field), cur[1].get(field)
                if a is not None and b is not None and b >= a:
                    out[key] = round((b - a) / dt, 1)
        old, cur = self._since(60)
        if old:
            a, b = old[1], cur[1]
            if a.get("ttft") and b.get("ttft"):
                p50 = hist_quantile(0.5, b["ttft"]["buckets"], a["ttft"]["buckets"])
                p95 = hist_quantile(0.95, b["ttft"]["buckets"], a["ttft"]["buckets"])
                out["ttft_p50"] = round(p50, 3) if p50 is not None else None
                out["ttft_p95"] = round(p95, 3) if p95 is not None else None
            for key, field, scale in (("e2e_avg", "e2e", 1.0), ("itl_ms", "itl", 1000.0)):
                ha, hb = a.get(field), b.get(field)
                if ha and hb and hb["count"] > ha["count"]:
                    out[key] = round(scale * (hb["sum"] - ha["sum"]) / (hb["count"] - ha["count"]), 3)
            qa, qb = a.get("prefix_queries"), b.get("prefix_queries")
            ha, hb = a.get("prefix_hits"), b.get("prefix_hits")
            if None not in (qa, qb, ha, hb) and qb > qa:
                out["prefix_hit_pct"] = round(100.0 * (hb - ha) / (qb - qa), 1)
        # tokens one decode step yields = 1 + accepted drafts per step; over
        # the last minute when there was decoding, else since the engine started
        if self.hist:
            cur = self.hist[-1][1]
            base = old[1] if old and (cur.get("spec_drafts") or 0) > (old[1].get("spec_drafts") or 0) else {}
            drafts = (cur.get("spec_drafts") or 0) - (base.get("spec_drafts") or 0)
            if drafts > 0:
                acc = (cur.get("spec_accepted") or 0) - (base.get("spec_accepted") or 0)
                proposed = (cur.get("spec_draft_tokens") or 0) - (base.get("spec_draft_tokens") or 0)
                out["tokens_per_step"] = round(1 + acc / drafts, 2)
                out["spec_accept_pct"] = round(100.0 * acc / proposed, 1) if proposed > 0 else None
                out["spec_window"] = "minute" if base else "start"
        return out

    def snapshot(self):
        d = {"kind": self.kind, "port": self.port, "models": self.models,
             "max_model_len": self.max_len, "ok": self.error is None}
        if self.error:
            d["error"] = self.error
        if self.kind == "ollama":
            d.update(self.extra)
            return d
        if self.hist:
            s = dict(self.hist[-1][1])
            for k in ("ttft", "e2e", "itl"):
                s.pop(k, None)
            if not d["models"]:
                d["models"] = s.get("models") or []
            s.pop("models", None)
            d.update(s)
            d.update(self.window())
        return d


def http_text(url, timeout=2.0, limit=4 << 20, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def http_json(url, timeout=2.0, headers=None):
    return json.loads(http_text(url, timeout=timeout, headers=headers))


def iface_ipv4(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name[:15].encode())
        import fcntl
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])   # SIOCGIFADDR
    except (OSError, ImportError):
        return None
    finally:
        s.close()


SKIP_IFACES = re.compile(r"^(lo|docker\d*|br-|veth|virbr|cni|flannel|cali|vxlan|kube|podman|tap|dummy)")


def iface_kind(name):
    if name.startswith(("tailscale", "ts", "wg")):
        return "tailnet"
    base = "/sys/class/net/" + name
    if os.path.exists(base + "/wireless") or os.path.exists(base + "/phy80211"):
        return "wifi"
    try:
        drv = os.path.basename(os.readlink(base + "/device/driver"))
    except OSError:
        drv = ""
    if drv.startswith(("mlx", "ice", "bnxt_en", "irdma")):
        return "fabric"       # ConnectX and friends: the node-to-node link
    return "lan"


ENGINE_PORTS_DEFAULT = "8888,8000,8001,8002,8080,30000,11434,1234"


class Node:
    def __init__(self):
        self.name = env("MONITOR_NAME", socket.gethostname())
        self.role = env("MONITOR_ROLE", "node")
        self.state_dir = env("MONITOR_STATE_DIR", "/run/rackmon")
        self.os_release_path = env("MONITOR_HOST_OS_RELEASE", "/run/host/os-release")
        self.engine_ports = sorted({int(p) for p in re.split(r"[,\s]+", env("MONITOR_ENGINE_PORTS", ENGINE_PORTS_DEFAULT)) if p.isdigit()})
        self.own_port = int(env("MONITOR_PORT", "9177"))
        self.lock = threading.Lock()
        self.prev_stat, self.prev_net, self.prev_t = None, None, None
        self.prev_cg = {}
        self.engines = {}
        self.engines_at = 0.0
        self.gpu_fields = None
        self.gpu_error = None
        self.static = self._static()
        self.history = collections.deque(maxlen=HISTORY_MAX)
        self.current = None

    # ---------------------------------------------------------- static facts
    def _static(self):
        osr = read(self.os_release_path) or read("/etc/os-release")
        u = os.uname()
        product = (read("/sys/class/dmi/id/product_family").strip()
                   or read("/sys/class/dmi/id/product_name").strip().replace("_", " ") or None)
        return {"os": parse_os_release(osr), "kernel": u.release, "arch": u.machine,
                "hostname": socket.gethostname(), "product": product,
                "cpu_model": parse_cpu_model(read("/proc/cpuinfo")),
                "cores": os.cpu_count()}

    # ------------------------------------------------------------------- gpu
    GPU_BASE = ["index", "name", "uuid", "utilization.gpu", "temperature.gpu", "power.draw",
                "clocks.sm", "clocks.max.sm", "pstate", "memory.used", "memory.total", "driver_version"]
    GPU_OPTIONAL = ["clocks_event_reasons.active", "clocks_throttle_reasons.active",
                    "utilization.memory", "fan.speed", "power.limit"]

    def _smi(self, args, timeout=5):
        p = subprocess.run(["nvidia-smi"] + args, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip()[:160] or "nvidia-smi exit %d" % p.returncode)
        return p.stdout

    def _probe_gpu_fields(self):
        self._smi(["--query-gpu=index", "--format=csv,noheader,nounits"])   # FileNotFoundError: no GPU stack
        fields = list(self.GPU_BASE)
        for f in self.GPU_OPTIONAL:
            if f == "clocks_throttle_reasons.active" and "clocks_event_reasons.active" in fields:
                continue
            try:
                self._smi(["--query-gpu=" + f, "--format=csv,noheader,nounits"])
                fields.append(f)
            except Exception:  # noqa: BLE001 - field unknown to this driver
                pass
        return fields

    def sample_gpus(self, containers):
        if self.gpu_fields is None:
            try:
                self.gpu_fields = self._probe_gpu_fields()
            except FileNotFoundError:
                self.gpu_fields = []
                self.gpu_error = "no nvidia-smi in the container: start it with --gpus all (rack monitor up does)"
            except Exception as e:  # noqa: BLE001
                self.gpu_error = str(e)[:160]
                return []
        if not self.gpu_fields:
            return []
        try:
            rows = parse_csv(self._smi(["--query-gpu=" + ",".join(self.gpu_fields),
                                        "--format=csv,noheader,nounits"]), self.gpu_fields)
            apps = parse_csv(self._smi(["--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                                        "--format=csv,noheader,nounits"]),
                             ["gpu_uuid", "pid", "process_name", "used_memory"])
            self.gpu_error = None
        except Exception as e:  # noqa: BLE001
            self.gpu_error = str(e)[:160]
            return []
        by_id = {c.get("_full"): c["name"] for c in containers if c.get("_full")}
        gpus = []
        for r in rows:
            reasons = decode_throttle(r.get("clocks_event_reasons.active") or r.get("clocks_throttle_reasons.active"))
            procs = []
            for a in apps:
                if a["gpu_uuid"] and r.get("uuid") and a["gpu_uuid"] != r["uuid"]:
                    continue
                pid = num(a["pid"])
                cmd = read("/proc/%s/cmdline" % pid) if pid else ""
                cid = container_of(read("/proc/%s/cgroup" % pid)) if pid else None
                mem = num(a["used_memory"])
                procs.append({"pid": pid, "name": proc_label(cmd, a["process_name"]),
                              "mem": mem * 1048576 if mem is not None else None,
                              "container": by_id.get(cid) if cid else None})
            mu, mt = num(r.get("memory.used")), num(r.get("memory.total"))
            gpus.append({
                "index": num(r.get("index")), "name": r.get("name"),
                "util": num(r.get("utilization.gpu")), "mem_util": num(r.get("utilization.memory")),
                "temp": num(r.get("temperature.gpu")), "power_w": num(r.get("power.draw")),
                "power_limit_w": num(r.get("power.limit")), "fan_pct": num(r.get("fan.speed")),
                "sm_clock": num(r.get("clocks.sm")), "sm_clock_max": num(r.get("clocks.max.sm")),
                "pstate": r.get("pstate") or None, "driver": r.get("driver_version") or None,
                "mem_used": mu * 1048576 if mu is not None else None,
                "mem_total": mt * 1048576 if mt is not None else None,
                "throttle": reasons, "throttled": bool(reasons and THROTTLE_BAD.intersection(reasons)),
                "procs": sorted(procs, key=lambda p: -(p["mem"] or 0)),
            })
        return gpus

    # ------------------------------------------------------------ containers
    def sample_containers(self, now, dt):
        f = os.path.join(self.state_dir, "containers.json")
        try:
            with open(f) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return [], "docker relay not running (no %s)" % f
        if rec.get("error"):
            return [], "docker relay: " + rec["error"]
        if now - float(rec.get("at") or 0) > 30:
            return [], "docker relay stale for %ds" % int(now - float(rec.get("at") or 0))
        out, seen = [], {}
        for c in rec.get("containers") or []:
            cid = c.get("id") or ""
            item = {"id": cid[:12], "name": c.get("name"), "image": c.get("image"),
                    "state": c.get("state"), "status": c.get("status"),
                    "project": c.get("project"), "cpu_cores": None, "mem": None}
            if c.get("state") == "running" and cid:
                for base in ("/sys/fs/cgroup/system.slice/docker-%s.scope" % cid, "/sys/fs/cgroup/docker/%s" % cid):
                    mem = num(read(base + "/memory.current").strip())
                    if mem is None:
                        continue
                    item["mem"] = mem
                    m = re.search(r"usage_usec (\d+)", read(base + "/cpu.stat"))
                    if m:
                        use = int(m.group(1))
                        prev = self.prev_cg.get(cid)
                        if prev is not None and dt and use >= prev:
                            item["cpu_cores"] = round((use - prev) / 1e6 / dt, 2)
                        seen[cid] = use
                    break
            item["_full"] = cid
            out.append(item)
        self.prev_cg = seen
        out.sort(key=lambda c: (c["state"] != "running", c["name"] or ""))
        return out, None

    # --------------------------------------------------------------- engines
    def discover_engines(self, now):
        if now - self.engines_at < 30 and self.engines_at:
            return
        self.engines_at = now
        listening = {}
        for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
            for ip, port in parse_listen(read(path), v6):
                if port in self.engine_ports and port != self.own_port:
                    if ip in ("0.0.0.0", "::", "::ffff:0.0.0.0") or ip.startswith("127.") or ip == "::1":
                        host = "127.0.0.1" if ip != "::1" else "::1"
                    elif ":" in ip:
                        continue
                    else:
                        host = ip
                    listening.setdefault(port, host)
        for port in list(self.engines):
            if port not in listening:
                del self.engines[port]
        for port, host in listening.items():
            if port in self.engines:
                continue
            kind = None
            try:
                kind = engine_kind(http_text("http://%s:%d/metrics" % (host, port), timeout=1.0, limit=8 << 20))
            except Exception:  # noqa: BLE001
                pass
            if not kind:
                try:
                    if "version" in http_json("http://%s:%d/api/version" % (host, port), timeout=1.0):
                        kind = "ollama"
                except Exception:  # noqa: BLE001
                    pass
            if not kind:
                try:
                    if isinstance(http_json("http://%s:%d/v1/models" % (host, port), timeout=1.0).get("data"), list):
                        kind = "openai"
                except Exception:  # noqa: BLE001
                    pass
            if kind:
                self.engines[port] = Engine(host, port, kind)

    # ---------------------------------------------------------------- system
    def sample_net(self, dt):
        cur = parse_net_dev(read("/proc/net/dev"))
        out = []
        for name, (rx, tx) in sorted(cur.items()):
            if SKIP_IFACES.match(name):
                continue
            oper = read("/sys/class/net/%s/operstate" % name).strip() or "unknown"
            ip = iface_ipv4(name)
            if oper == "down" and not ip:
                continue
            speed = num(read("/sys/class/net/%s/speed" % name).strip())
            item = {"iface": name, "kind": iface_kind(name), "ip": ip, "up": oper in ("up", "unknown"),
                    "speed_mbps": speed if speed and speed > 0 else None, "rx_bps": None, "tx_bps": None}
            prev = (self.prev_net or {}).get(name)
            if prev and dt:
                if rx >= prev[0]:
                    item["rx_bps"] = round((rx - prev[0]) / dt)
                if tx >= prev[1]:
                    item["tx_bps"] = round((tx - prev[1]) / dt)
            out.append(item)
        self.prev_net = cur
        return out

    def sample_temps(self):
        groups = {"acpitz": "soc", "nvme": "nvme", "mlx5": "nic", "coretemp": "cpu", "k10temp": "cpu",
                  "zenpower": "cpu", "cpu_thermal": "cpu", "soc_thermal": "soc"}
        out = {}
        root = "/sys/class/hwmon"
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for h in names:
            g = groups.get(read("%s/%s/name" % (root, h)).strip())
            if not g:
                continue
            try:
                files = [x for x in os.listdir("%s/%s" % (root, h)) if re.match(r"temp\d+_input$", x)]
            except OSError:
                continue
            for fn in files:
                v = num(read("%s/%s/%s" % (root, h, fn)).strip())
                if v is not None and 0 < v < 150000:
                    out[g] = max(out.get(g, 0), round(v / 1000.0, 1))
        return out

    def sample(self):
        t0 = time.time()
        now = t0
        dt = (now - self.prev_t) if self.prev_t else None
        stat = parse_proc_stat(read("/proc/stat"))
        cores = []
        if self.prev_stat:
            for i in range(len(stat) - 1):
                k = "cpu%d" % i
                if k in stat:
                    cores.append(busy_pct(self.prev_stat.get(k), stat[k]))
        cpu = {"pct": busy_pct((self.prev_stat or {}).get("cpu"), stat.get("cpu")),
               "cores_pct": cores, "load": parse_loadavg(read("/proc/loadavg"))}
        self.prev_stat = stat
        mi = parse_meminfo(read("/proc/meminfo"))
        total, avail = mi.get("MemTotal"), mi.get("MemAvailable")
        mem = {"total": total, "available": avail,
               "used": (total - avail) if total and avail is not None else None,
               "cached": mi.get("Cached"), "swap_total": mi.get("SwapTotal"),
               "swap_used": (mi["SwapTotal"] - mi.get("SwapFree", 0)) if mi.get("SwapTotal") else 0}
        containers, containers_error = self.sample_containers(now, dt)
        gpus = self.sample_gpus(containers)
        gpu_proc_mem = sum(p["mem"] or 0 for g in gpus for p in g["procs"])
        mem["gpu_procs"] = gpu_proc_mem if gpus else None
        self.discover_engines(now)
        for e in self.engines.values():
            e.poll(now)
        engines = [e.snapshot() for _, e in sorted(self.engines.items())]
        net = self.sample_net(dt)
        try:
            st = os.statvfs("/")
            disks = [{"mount": "/", "total": st.f_blocks * st.f_frsize,
                      "used": (st.f_blocks - st.f_bfree) * st.f_frsize}]
        except OSError:
            disks = []
        up_text = read("/proc/uptime").split()
        uptime = num(up_text[0]) if up_text else None
        temps = self.sample_temps()
        if gpus and gpus[0].get("temp") is not None:
            temps["gpu"] = max(g["temp"] for g in gpus if g.get("temp") is not None)
        for c in containers:
            c.pop("_full", None)
        system = dict(self.static, uptime_s=int(uptime) if uptime else None,
                      unified_memory=bool(gpus) and all(g["mem_total"] is None for g in gpus))
        snap = {
            "schema": SCHEMA, "name": self.name, "role": self.role, "ok": True,
            "version": VERSION, "sampled_at": round(now, 3),
            "system": system, "cpu": cpu, "mem": mem, "gpus": gpus,
            "gpu_error": self.gpu_error, "temps": temps, "disks": disks, "net": net,
            "engines": engines, "containers": containers, "containers_error": containers_error,
        }

        def by_kind(kind):
            v = [n for n in net if n["kind"] == kind and n["rx_bps"] is not None]
            return sum((n["rx_bps"] or 0) + (n["tx_bps"] or 0) for n in v) if v else None
        utils = [g["util"] for g in gpus if g.get("util") is not None]
        powers = [g["power_w"] for g in gpus if g.get("power_w") is not None]
        gens = [e.get("gen_tps") for e in engines if e.get("gen_tps") is not None]
        prompts = [e.get("prompt_tps") for e in engines if e.get("prompt_tps") is not None]
        kvs = [e.get("kv_pct") for e in engines if e.get("kv_pct") is not None]
        point = {
            "t": round(now, 1), "cpu": cpu["pct"],
            "mem": round(mem["used"] / 1e9, 2) if mem["used"] is not None else None,
            "gpu": round(sum(utils) / len(utils), 1) if utils else None,
            "temp": temps.get("gpu"), "power": round(sum(powers), 1) if powers else None,
            "gen_tps": round(sum(gens), 1) if gens else None,
            "prompt_tps": round(sum(prompts), 1) if prompts else None,
            "kv": max(kvs) if kvs else None,
            "fabric": by_kind("fabric"), "lan": by_kind("lan"), "tailnet": by_kind("tailnet"),
        }
        snap["sample_ms"] = round((time.time() - t0) * 1000)
        with self.lock:
            self.current = snap
            if dt:
                self.history.append(point)
        self.prev_t = now
        return snap

    def snapshot(self, history=0):
        with self.lock:
            snap = dict(self.current) if self.current else {"schema": SCHEMA, "name": self.name, "role": self.role,
                                                             "ok": False, "error": "warming up"}
            if history:
                pts = list(self.history)[-history:]
                keys = ("t", "cpu", "mem", "gpu", "temp", "power", "gen_tps", "prompt_tps", "kv",
                        "fabric", "lan", "tailnet")
                snap["history"] = {k: [p.get(k) for p in pts] for k in keys}
        return snap


# ===================================================================== serve ==
class App:
    def __init__(self, node, token):
        self.node = node
        self.token = token
        self.cluster_name = env("MONITOR_CLUSTER", "rack")
        self.peers = []
        for item in [x for x in re.split(r"[,\s]+", env("MONITOR_PEERS", "")) if x]:
            name, sep, url = item.partition("=")
            if not sep or not url.startswith(("http://", "https://")):
                print("rack-monitor: ignoring malformed peer %r (want name=http://host:port)" % item, file=sys.stderr)
                continue
            self.peers.append((name, url.rstrip("/")))
        self.peer_token = env("MONITOR_PEER_TOKEN", token)
        self.pool = ThreadPoolExecutor(max_workers=max(2, len(self.peers)))
        self.cache = {}
        self.cache_lock = threading.Lock()

    def hello(self):
        return {"service": "rack-monitor", "version": VERSION, "schema": SCHEMA, "name": self.node.name,
                "role": self.node.role, "cluster": self.cluster_name, "peers": len(self.peers),
                "auth": "bearer"}

    def _peer(self, name, url, history):
        key = (name, history)
        with self.cache_lock:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < 1.5:
                return hit[1]
        t0 = time.time()
        try:
            d = http_json("%s/v1/node?history=%d" % (url, history), timeout=2.5,
                          headers={"Authorization": "Bearer " + self.peer_token})
            if d.get("schema") != SCHEMA:
                raise ValueError("peer speaks schema %r, this head speaks %d" % (d.get("schema"), SCHEMA))
            d["name"] = d.get("name") or name
            d["latency_ms"] = round((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            d = {"schema": SCHEMA, "name": name, "ok": False, "role": "peer",
                 "error": "peer answered HTTP %d%s" % (e.code, " (token mismatch)" if e.code == 401 else "")}
        except Exception as e:  # noqa: BLE001
            d = {"schema": SCHEMA, "name": name, "ok": False, "role": "peer", "error": str(e)[:160]}
        with self.cache_lock:
            self.cache[key] = (time.time(), d)
        return d

    def cluster(self, history):
        futures = [self.pool.submit(self._peer, n, u, history) for n, u in self.peers]
        nodes = [self.node.snapshot(history)] + [f.result() for f in futures]
        return {"service": "rack-monitor", "version": VERSION, "schema": SCHEMA,
                "cluster": self.cluster_name, "head": self.node.name,
                "sampled_at": round(time.time(), 3), "nodes": nodes}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rack-monitor/" + VERSION
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, code, obj, extra=None):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            h = self.headers.get("Authorization", "")
            given = ""
            if h[:7].lower() == "bearer ":
                given = h[7:].strip()
            elif h[:6].lower() == "basic ":
                try:
                    given = base64.b64decode(h[6:].strip()).decode("utf-8", "replace").partition(":")[2]
                except (ValueError, UnicodeDecodeError):
                    given = ""
            return bool(app.token) and hmac.compare_digest(given.encode(), app.token.encode())

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path in ("/", "/v1/hello"):
                return self._send(200, app.hello())
            if u.path not in ("/v1/node", "/v1/cluster"):
                return self._send(404, {"error": "not found", "try": ["/v1/hello", "/v1/node", "/v1/cluster"]})
            if not self._authed():
                time.sleep(0.25)
                return self._send(401, {"error": "token required: Authorization: Bearer <rack monitor token>"},
                                  {"WWW-Authenticate": 'Basic realm="rack-monitor"'})
            q = urllib.parse.parse_qs(u.query)
            try:
                history = max(0, min(HISTORY_MAX, int((q.get("history") or ["0"])[0])))
            except ValueError:
                history = 0
            if u.path == "/v1/node":
                return self._send(200, app.node.snapshot(history))
            return self._send(200, app.cluster(history))

        def do_POST(self):
            self._send(405, {"error": "read-only"})

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def load_token():
    tok = env("MONITOR_TOKEN")
    path = env("MONITOR_TOKEN_FILE", "/run/secrets/rack-monitor-token")
    if not tok and os.path.exists(path):
        tok = read(path).strip()
    return tok


def sampler_loop(node, step):
    while True:
        t0 = time.time()
        try:
            node.sample()
        except Exception as e:  # noqa: BLE001 - one bad sample must not stop the monitor
            print("rack-monitor: sample failed: %s" % e, file=sys.stderr)
        time.sleep(max(0.2, step - (time.time() - t0)))


def serve():
    token = load_token()
    if not token and env("MONITOR_INSECURE") != "1":
        sys.exit("rack-monitor: no token (MONITOR_TOKEN_FILE or MONITOR_TOKEN); refusing to serve open telemetry")
    node = Node()
    node.sample()                       # first sample primes the counters
    step = float(env("MONITOR_SAMPLE_S", "2"))
    threading.Thread(target=sampler_loop, args=(node, step), daemon=True).start()
    app = App(node, token)
    port = int(env("MONITOR_PORT", "9177"))
    servers = []
    for addr in [a for a in re.split(r"[,\s]+", env("MONITOR_BIND", "0.0.0.0")) if a]:
        srv = ThreadingHTTPServer((addr, port), make_handler(app))
        srv.daemon_threads = True
        servers.append(srv)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("rack-monitor %s: %s (%s) on %s:%d, peers: %s" % (
        VERSION, node.name, node.role, ",".join(s.server_address[0] for s in servers), port,
        ", ".join(n for n, _ in app.peers) or "none"), file=sys.stderr, flush=True)
    servers[0].serve_forever()


# ============================================================== docker relay ==
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5.0):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.unix_path)
        except OSError:
            s.close()
            raise
        self.sock = s


def slim_container(c):
    """Only what the dashboard shows. No env, no command line, no labels
    beyond the compose project: those carry secrets often enough."""
    return {"id": c.get("Id"), "name": ((c.get("Names") or ["?"])[0] or "?").lstrip("/"),
            "image": c.get("Image"), "state": c.get("State"), "status": c.get("Status"),
            "created": c.get("Created"),
            "project": (c.get("Labels") or {}).get("com.docker.compose.project")}


def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def docker_relay():
    sock = env("DOCKER_SOCK", "/var/run/docker.sock")
    out = os.path.join(env("MONITOR_STATE_DIR", "/run/rackmon"), "containers.json")
    step = float(env("RELAY_INTERVAL_S", "5"))
    os.umask(0o077)
    print("rack-monitor docker relay %s: %s -> %s every %gs" % (VERSION, sock, out, step), file=sys.stderr, flush=True)
    while True:
        rec = relay_once(sock, out)
        time.sleep(step if "error" not in rec else max(step, 10))


def relay_once(sock, out):
    """One GET /containers/json over the Docker socket, slimmed, written
    atomically. The only Docker API call anything in the monitor makes."""
    rec = {"at": round(time.time(), 3)}
    conn = UnixHTTPConnection(sock)
    try:
        conn.request("GET", "/containers/json?all=1")
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError("docker answered HTTP %d" % r.status)
        rec["containers"] = [slim_container(c) for c in json.loads(body)]
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)[:200]
    finally:
        conn.close()
    try:
        write_atomic(out, rec)
    except OSError as e:
        print("rack-monitor relay: cannot write %s: %s" % (out, e), file=sys.stderr, flush=True)
    return rec


def main(argv):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    mode = argv[1] if len(argv) > 1 else "serve"
    if mode == "serve":
        return serve()
    if mode == "docker-relay":
        return docker_relay()
    if mode == "once":
        node = Node()
        node.sample()
        time.sleep(1.0)
        print(json.dumps(node.sample(), indent=1))
        return 0
    if mode in ("version", "--version"):
        print(VERSION)
        return 0
    sys.exit("usage: rackmon.py serve | docker-relay | once | version")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
