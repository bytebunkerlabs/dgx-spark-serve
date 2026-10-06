"""rack up and rack down: plan what runs where, then run it. Stdlib only.

    python3 py/serve.py up --recipe-name N --recipe-file F --recipe-dir D \
        --platform P --facts FACTS.json [--plan] [--json] [--replace] [--boot]
    python3 py/serve.py down [--plan] [--json]

A plan is data: every step names the node it runs on and exactly what runs
there, so `rack up --plan --json` shows a 1, 2 or 4 node launch without
touching a machine, and the tests check plans for every platform. rack
passes the site in the environment: RACK_ROOT, DGX_SERVE_CONFIG,
DGX_SERVE_STATE, HF_CACHE, API_PORT, SITE_IMAGE, ENGINE_BIND, ENGINE_KEY,
MASTER_PORT, MEM_CAP_GB, FOREIGN_ENGINES, MEMWATCH_KILL, TOPOLOGY, and
RACK_NODES (one line per node, the head first: name, ssh, fabric ip,
fabric interface, IB HCAs, rack dir, HF cache, platform, GPUs).

Serving is always detached. vLLM runs in a container with
--restart unless-stopped (a cluster as one keep-alive container per node,
the head's boot unit re-forming it after a reboot); llama.cpp, and vLLM on
Windows without Docker, run natively under launchd (Mac) or a systemd user
unit (Linux, WSL2). The launcher owns the host, the port, the served name
and the API key, and caps a container's memory from the machine's total.
"""
import argparse
import datetime
import json
import os
import plistlib
import shlex
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engines  # noqa: E402
import recipes  # noqa: E402

LABEL = "ai.bytebunker.dgx-serve.engine"          # launchd job
UNIT = "dgx-serve-engine.service"                 # systemd user unit
BOOT_UNIT = "dgx-serve-boot.service"              # re-forms a cluster after a reboot
SOLO, NODE = "serve_solo", "serve_node"           # container names
ENGINE_LABEL = "ai.bytebunker.dgx-serve=engine"
# GiB a machine keeps for itself under a container's memory cap
MEM_RESERVE_GB = {"dgx": 9, "linux": 8, "windows": 4}
# What a plan assumes about a machine it cannot see (--plan for another platform)
ASSUMED_FACTS = {
    "dgx": {"os": "linux", "arch": "aarch64", "memory_mb": 124610, "cuda": "13.0", "init": "systemd",
            "docker": True, "gpus": [{"name": "NVIDIA GB10", "memory_mb": 0}]},
    "linux": {"os": "linux", "arch": "x86_64", "memory_mb": 65536, "cuda": "12.9", "init": "systemd",
              "docker": True, "gpus": [{"name": "NVIDIA GeForce RTX 4090", "memory_mb": 24564}]},
    "windows": {"os": "linux", "arch": "x86_64", "memory_mb": 32768, "cuda": "12.9", "init": "systemd",
                "wsl": True, "docker": False, "gpus": [{"name": "NVIDIA GeForce RTX 2070", "memory_mb": 8192}]},
    "mac": {"os": "darwin", "arch": "arm64", "memory_mb": 16384, "metal_budget_mb": 10922, "init": "launchd",
            "docker": False, "gpus": []},
}


class PlanError(Exception):
    pass


# ----------------------------------------------------------------- the site --
class Node:
    def __init__(self, fields, local):
        f = (fields + [""] * 9)[:9]
        self.name, self.ssh, self.fabric_ip, self.fabric_if, self.hcas = f[0], f[1], f[2], f[3], f[4]
        self.rack_dir, self.hf_cache, self.platform = f[5], f[6], f[7]
        self.gpus = int(f[8]) if f[8].isdigit() else 0
        self.local = local

    def home(self, rel):
        """A path under the node's home: absolute here, ~/... over ssh."""
        return os.path.join(os.path.expanduser("~"), rel) if self.local else "~/" + rel

    def json(self):
        return {"name": self.name, "where": "local" if self.local else self.ssh,
                "fabric_ip": self.fabric_ip or None}


class Site:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.root = e.get("RACK_ROOT") or os.getcwd()
        home = os.path.expanduser("~")
        self.config = e.get("DGX_SERVE_CONFIG") or os.path.join(home, ".config", "dgx-serve")
        self.state = e.get("DGX_SERVE_STATE") or os.path.join(home, ".local", "state", "dgx-serve")
        self.hf_cache = e.get("HF_CACHE") or os.path.join(home, "dgx", "hf")
        self.port = int(e.get("API_PORT") or 8888)
        self.site_image = e.get("SITE_IMAGE") or ""
        self.key = (e.get("ENGINE_KEY") or "on").lower() not in ("off", "0", "no", "none")
        self.bind = e.get("ENGINE_BIND") or ("0.0.0.0" if self.key else "127.0.0.1")
        self.master_port = int(e.get("MASTER_PORT") or 29501)
        self.mem_cap = e.get("MEM_CAP_GB", "")
        self.foreign = (e.get("FOREIGN_ENGINES") or "").split()
        self.memwatch_kill = e.get("MEMWATCH_KILL") or "0"
        self.topology = e.get("TOPOLOGY") or "auto"
        self.version = e.get("RACK_VERSION") or ""
        self.debug = e.get("RACK_DEBUG") == "1"
        lines = [l for l in (e.get("RACK_NODES") or "").split("\n") if l.strip()]
        self.nodes = [Node(l.split("\t"), i == 0) for i, l in enumerate(lines)]
        if not self.nodes:
            self.nodes = [Node([e.get("HEAD_LABEL") or "this-node", "", "", "", "", self.root, self.hf_cache,
                                "", "0"], True)]
        self.key_file = os.path.join(self.config, "engine.key")
        self.env_file = os.path.join(self.config, "engine.env")

    @property
    def head(self):
        return self.nodes[0]


# ------------------------------------------------------------------- steps --
def step(node, kind, what, **kw):
    d = {"node": node.name, "kind": kind, "what": what}
    d.update(kw)
    return d


def run(node, argv, what, ignore_errors=False, **kw):
    return step(node, "run", what, argv=[str(a) for a in argv], ignore_errors=ignore_errors, **kw)


def hub_dir(cache, repo):
    return os.path.join(cache, "hub", "models--" + repo.replace("/", "--"))


# ---------------------------------------------------------------- planning --
def load_recipe(name, file, recipe_dir, platform, root):
    flat = os.path.basename(file) not in ("dgx.env", "linux.env", "windows.env", "mac.env")
    v = recipes.source(file, recipe_dir, root)
    d = recipes.describe(v, platform, flat)
    return v, d, flat


def served_name(v, args, name, flat):
    if flat:
        n = recipes.flag(args, "--served-model-name")
        if isinstance(n, str):
            return n
    return v.get("SERVED_NAME") or name


def host_port(site, args, flat, notes):
    """The launcher owns these; a recipe from before 1.0 that sets them is honoured."""
    host, port = site.bind, site.port
    if flat:
        h, p = recipes.flag(args, "--host"), recipes.flag(args, "--port")
        if isinstance(h, str):
            host = h
            notes.append("this recipe sets --host %s itself (before 1.0); rack honours it" % h)
        if isinstance(p, str) and p.isdigit():
            port = int(p)
            notes.append("this recipe sets --port %s itself (before 1.0); rack honours it" % p)
    return host, port


def strip_owned(args):
    """SERVE_ARGS without the flags the launcher adds itself."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("--host", "--port", "--served-model-name", "--api-key", "--api-key-file", "--alias"):
            skip = True
            continue
        if a.split("=", 1)[0] in ("--host", "--port", "--served-model-name", "--api-key", "--api-key-file", "--alias"):
            continue
        out.append(a)
    return out


def mem_cap_gb(site, platform, facts):
    if site.mem_cap not in ("", None):
        return int(site.mem_cap) or None          # MEM_CAP_GB=0 turns the cap off
    total = (facts.get("memory_mb") or 0) // 1024
    reserve = MEM_RESERVE_GB.get(platform)
    if not total or reserve is None:
        return None
    return max(4, total - reserve)


def plan_up(site, recipe_name, recipe_file, recipe_dir, platform, facts, replace=False, boot=False, assumed=False):
    v, d, flat = load_recipe(recipe_name, recipe_file, recipe_dir, platform, site.root)
    unanswered = sorted(k for k, val in v.items() if "FILL_ME" in (" ".join(val) if isinstance(val, list) else val))
    if unanswered:
        raise PlanError("%s still has FILL_ME in %s: answer its questions first (docs/05-recipe-method.md)"
                        % (recipe_file, ", ".join(unanswered)))
    engine = d["engine"]
    if engine not in ("vllm", "llamacpp"):
        raise PlanError("%s: ENGINE must be vllm or llamacpp (got: %s)" % (recipe_name, engine))
    model = v.get("MODEL") or ""
    args = v.get("SERVE_ARGS", [])
    notes = []
    host, port = host_port(site, args, flat, notes)
    name = served_name(v, args, recipe_name, flat)
    if not site.key:
        notes.append("ENGINE_KEY=off: the engine takes requests without a key, so it listens on %s only "
                     "unless ENGINE_BIND says otherwise" % site.bind)
    if engine == "llamacpp":
        runtime = "launchd" if platform == "mac" else "systemd"
    elif platform in ("dgx", "linux"):
        runtime = "docker"
    elif platform == "windows":
        runtime = "docker" if facts.get("docker") and facts.get("nvidia_container_runtime") else "systemd"
    else:
        raise PlanError("vLLM does not serve on a Mac: give %s a mac variant with ENGINE=llamacpp" % recipe_name)

    p = {"schema": 1, "command": "up", "recipe": recipe_name, "variant": recipe_file, "platform": platform,
         "engine": engine, "runtime": runtime, "model": model, "served_name": name,
         "artifact": v.get("ARTIFACT") or None, "host": host, "port": port, "key_required": site.key,
         "assumed_facts": assumed, "notes": notes, "steps": []}
    common = {"v": v, "d": d, "flat": flat, "args": args, "host": host, "port": port, "name": name,
              "model": model, "engine": engine, "recipe": recipe_name, "platform": platform}
    if runtime == "docker":
        nodes = nodes_needed(site, d, platform, facts)
        p["nodes"] = [n.json() for n in site.nodes[:nodes]]
        p["memory_cap_gb"] = mem_cap_gb(site, platform, facts)
        if nodes > 1:
            plan_cluster(site, p, common, site.nodes[:nodes], facts, replace, boot)
        else:
            plan_docker_solo(site, p, common, facts, replace)
    else:
        p["nodes"] = [site.head.json()]
        p["memory_cap_gb"] = mem_cap_gb(site, platform, facts) if runtime == "systemd" else None
        plan_native(site, p, common, facts, runtime, replace)
    p["serving"] = serving_record(site, p, common)
    p["steps"].append(step(site.head, "record", "record what is serving",
                           path=os.path.join(site.state, "serving.json"), content=p["serving"]))
    return p


def nodes_needed(site, d, platform, facts):
    tp, pp = d.get("tensor_parallel", 1), d.get("pipeline_parallel", 1)
    if site.topology == "solo":
        return 1
    if platform == "dgx":
        need = tp * pp                              # one GPU per Spark
    else:
        gpus = len(facts.get("gpus") or []) or 1
        if tp > gpus:
            raise PlanError("tensor parallel %d needs %d GPUs in one machine; this one has %d "
                            "(lower --tensor-parallel-size, or add --pipeline-parallel-size across machines)"
                            % (tp, tp, gpus))
        need = pp
    if site.topology == "cluster":
        need = max(need, 2)
    have = len(site.nodes)
    if need > have:
        raise PlanError("this recipe spans %d machines and the rack has %d: rack nodes add <worker> --fabric <ip>"
                        " (or TOPOLOGY=solo for one machine with several GPUs)" % (need, have))
    return need


def locality_env():
    # local means local: weights come from the mounted cache, vLLM's usage
    # stats stay off, and the hub client never phones home. rack net verifies.
    return ["PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True", "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
            "VLLM_NO_USAGE_STATS=1", "DO_NOT_TRACK=1"]


def docker_common(site, node, recipe, platform, facts, cap, head):
    a = ["--label", ENGINE_LABEL, "--label", "ai.bytebunker.dgx-serve.recipe=" + recipe,
         "--network", "host", "--gpus", "all", "--ipc=host", "--ulimit", "nofile=1048576:1048576"]
    if cap:
        a += ["--memory=%dg" % cap, "--memory-swap=%dg" % cap]
    if facts.get("init") == "systemd":
        a += ["--log-driver", "journald"]           # the log outlives the container
    else:
        a += ["--log-opt", "max-size=100m", "--log-opt", "max-file=3"]
    if head and site.key:
        a += ["--env-file", site.env_file]          # VLLM_API_KEY, from rack init
    for kv in locality_env():
        a += ["-e", kv]
    hf = node.hf_cache or site.hf_cache
    a += ["-v", hf + ":/root/.cache/huggingface",
          "-v", node.home(".cache/vllm") + ":/root/.cache/vllm",
          "-v", node.home(".cache/flashinfer") + ":/root/.cache/flashinfer",
          "-v", node.home(".triton") + ":/root/.triton"]
    return a


def image_of(site, v, platform):
    return v.get("IMAGE") or site.site_image or engines.VLLM_IMAGE.get(platform, "")


def guard_steps(site, nodes, names, replace, check_port, port):
    """Refuse to start on top of another engine; with --replace, stop rack's own first."""
    out = []
    for n in nodes:
        if replace:
            out.append(run(n, ["docker", "rm", "-f", SOLO, NODE], "stop the engine rack started before",
                           ignore_errors=True, quiet=True))
        out.append(step(n, "free", "nothing else serving here",
                        containers=names + site.foreign, label=ENGINE_LABEL,
                        port=port if (check_port and n.local) else None))
    return out


def plan_docker_solo(site, p, c, facts, replace):
    h = site.head
    v, args = c["v"], c["args"]
    image = image_of(site, v, c["platform"])
    p["image"] = image
    steps = p["steps"]
    steps += guard_steps(site, [h], [SOLO, NODE], replace, True, c["port"])
    steps.append(step(h, "image", "the engine image is here", image=image,
                      build=image == engines.VLLM_IMAGE["dgx"] and not v.get("IMAGE") and not site.site_image))
    steps.append(step(h, "weights", "the weights are here", repo=c["model"], cache=site.hf_cache,
                      recipe=c["recipe"], platform=c["platform"]))
    if c["platform"] == "dgx":
        steps.append(drop_caches(h))
    steps.append(run(h, ["mkdir", "-p", h.home(".cache/vllm"), h.home(".cache/flashinfer"), h.home(".triton")],
                     "caches the container writes to"))
    steps.append(run(h, ["docker", "rm", "-f", SOLO], "a stopped run of before", ignore_errors=True, quiet=True))
    mounts = []
    for m in v.get("MODS", []):
        if not m:
            continue
        overlay = os.path.join(site.root, m, "overlay")
        if not os.path.isdir(overlay):
            raise PlanError("mod has no overlay/ dir: %s" % m)
        for dirpath, _, files in os.walk(overlay):
            for fn in sorted(files):
                src = os.path.join(dirpath, fn)
                mounts += ["-v", "%s:%s:ro" % (src, src[len(overlay):])]
    env = []
    for kv in v.get("ENV_EXTRA", []):
        if kv:
            env += ["-e", kv]
    cmd = ["vllm", "serve", c["model"]] + strip_owned(args) + [
        "--host", c["host"], "--port", str(c["port"]), "--served-model-name", c["name"]]
    argv = (["docker", "run", "-d", "--name", SOLO, "--restart", "unless-stopped"]
            + docker_common(site, h, c["recipe"], c["platform"], facts, p.get("memory_cap_gb"), True)
            + env + mounts + ["--entrypoint=", image] + cmd)
    steps.append(run(h, argv, "start %s (detached; restarts with the machine)" % c["recipe"]))
    if c["platform"] == "dgx":
        steps += memwatch_steps(site, [h], SOLO)
    steps.append(wait_step(site, h, c["port"], ["docker", "logs", "-f", "--tail", "20", SOLO],
                           ["docker", "inspect", "-f", "{{.State.Running}}", SOLO], "true"))


def drop_caches(n):
    # On unified memory, cached file pages and GPU allocations share one pool:
    # reclaim before a big load. A sudoers drop-in may allow exactly
    # /usr/local/sbin/drop-caches (docs/12-platforms.md); the sh -c form is the
    # fallback. Silence here once wedged a rack, so it warns.
    return run(n, ["sh", "-c", "sudo -n /usr/local/sbin/drop-caches 2>/dev/null || "
                               "sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null || "
                               "echo 'WARN: could not drop caches (no passwordless sudo): docs/12-platforms.md' >&2"],
               "drop the page cache before a big load", ignore_errors=True)


def memwatch_steps(site, nodes, container):
    src = os.path.join(site.root, "scripts", "memwatch.sh")
    try:
        content = open(src).read()
    except OSError:
        return []
    out = []
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in nodes:
        state = site.state if n.local else "~/.local/state/dgx-serve"
        script = os.path.join(state, "bin", "memwatch.sh")
        log = os.path.join(state, "logs", "memwatch-%s-%s.log" % (container, stamp))
        out.append(step(n, "write", "the memory watchdog", path=script, content=content, mode="0755"))
        # [m]emwatch: the pattern must not match the shell that runs pkill
        out.append(run(n, ["pkill", "-f", "[m]emwatch.sh %s" % container], "an older watchdog",
                       ignore_errors=True, quiet=True))
        out.append(step(n, "spawn", "watch memory pressure (MEMWATCH_KILL=%s)" % site.memwatch_kill,
                        argv=["env", "MEMWATCH_KILL=%s" % site.memwatch_kill, "bash", script, container, "3"],
                        log=log))
    return out


def wait_step(site, h, port, logs, alive, alive_value, timeout=3600):
    return step(h, "wait", "wait until it answers", url="http://127.0.0.1:%d/health" % port, logs=logs,
                alive=alive, alive_value=alive_value, timeout=timeout)


def plan_cluster(site, p, c, nodes, facts, replace, boot):
    v, args = c["v"], c["args"]
    head = nodes[0]
    if not head.fabric_ip:
        raise PlanError("the head (%s) has no fabric address: rack init --fabric <ip>" % head.name)
    for n in nodes[1:]:
        if not n.fabric_ip:
            raise PlanError("%s has no fabric address: rack nodes add %s --fabric <ip>" % (n.name, n.name))
        if n.platform and n.platform != c["platform"]:
            raise PlanError("%s is %s, not %s: one model across machines needs the same platform on each"
                            % (n.name, n.platform, c["platform"]))
    image = image_of(site, v, c["platform"])
    p["image"] = image
    p["master"] = "%s:%d" % (head.fabric_ip, site.master_port)
    steps = p["steps"]
    if boot:
        steps.append(step(head, "reach", "every worker answers over ssh (after a reboot they take a while)",
                          nodes=[n.name for n in nodes[1:]], timeout=600))
    steps += guard_steps(site, nodes, [SOLO, NODE], replace or boot, True, c["port"])
    steps.append(step(head, "same-image", "the same engine image on every node", image=image,
                      nodes=[n.name for n in nodes]))
    steps.append(step(head, "weights", "the weights on every node", repo=c["model"], cache=site.hf_cache,
                      recipe=c["recipe"], platform=c["platform"], nodes=[n.name for n in nodes]))
    if c["platform"] == "dgx":
        steps += [drop_caches(n) for n in nodes]
    total = len(nodes)
    for rank, n in enumerate(nodes):
        steps.append(run(n, ["mkdir", "-p", n.home(".cache/vllm"), n.home(".cache/flashinfer"), n.home(".triton")],
                         "caches the container writes to"))
        steps.append(run(n, ["docker", "rm", "-f", NODE], "a container left by a launch that died",
                         ignore_errors=True, quiet=True))
        env = ["-e", "VLLM_HOST_IP=" + n.fabric_ip]
        iface = n.fabric_if or head.fabric_if
        if iface:
            for k in ("NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "TP_SOCKET_IFNAME", "UCX_NET_DEVICES"):
                env += ["-e", "%s=%s" % (k, iface)]
        hcas = n.hcas or head.hcas
        # BOTH RoCE twins of the cabled port, or NCCL caps at one PCIe rail.
        # No NCCL_IB_GID_INDEX, ever: NCCL >= 2.21 picks the RoCEv2 GID itself.
        env += (["-e", "NCCL_IB_HCA=" + hcas, "-e", "NCCL_IB_DISABLE=0"] if hcas else ["-e", "NCCL_IB_DISABLE=1"])
        env += ["-e", "NCCL_IGNORE_CPU_AFFINITY=1"]
        if site.debug:
            env += ["-e", "NCCL_DEBUG=INFO"]
        for kv in v.get("ENV_EXTRA", []):
            if kv:
                env += ["-e", kv]
        argv = (["docker", "run", "-d", "--name", NODE, "--restart", "unless-stopped", "--privileged"]
                + docker_common(site, n, c["recipe"], c["platform"], facts, p.get("memory_cap_gb"), rank == 0)
                + env + ["--entrypoint=", image, "sleep", "infinity"])
        steps.append(run(n, argv, "rank %d's container on %s, idle until the launch" % (rank, n.name)))
    steps.append(run(head, ["docker", "exec", NODE, "python3", "-c",
                            "from vllm.engine.arg_utils import EngineArgs; assert hasattr(EngineArgs, 'nnodes')"],
                     "this image's vLLM spans machines natively (>= 0.26)",
                     fail="this image's vLLM lacks native multi-node (EngineArgs.nnodes): rack build --profile upstream"))
    for m in v.get("MODS", []):
        if not m:
            continue
        src = os.path.join(site.root, m)
        if not os.path.isdir(src):
            raise PlanError("mod missing: %s" % m)
        for n in nodes:
            steps.append(step(n, "mod", "apply mod %s" % os.path.basename(m), container=NODE, src=src,
                              name=os.path.basename(m.rstrip("/"))))
    if c["platform"] == "dgx":
        steps += memwatch_steps(site, nodes, NODE)
    for rank, n in sorted(enumerate(nodes), key=lambda x: -x[0]):    # workers first, the head last
        cmd = ["vllm", "serve", c["model"]] + strip_owned(args) + [
            "--nnodes", str(total), "--node-rank", str(rank),
            "--master-addr", head.fabric_ip, "--master-port", str(site.master_port)]
        cmd += ["--headless"] if rank else ["--host", c["host"], "--port", str(c["port"]),
                                             "--served-model-name", c["name"]]
        script = "#!/bin/bash\nset -e\nexec " + " ".join(shlex.quote(x) for x in cmd) + "\n"
        steps.append(run(n, ["docker", "exec", "-w", "/", NODE, "mkdir", "-p", "/workspace"], "a place for the script"))
        steps.append(step(n, "write", "rank %d's launch script" % rank, container=NODE,
                          path="/workspace/launch.sh", content=script, mode="0755"))
        steps.append(run(n, ["docker", "exec", "-d", NODE, "bash", "-c", "bash /workspace/launch.sh >> /proc/1/fd/1 2>&1"],
                         "launch rank %d%s" % (rank, " (headless)" if rank else ", the API server")))
    steps += boot_unit_steps(site, head, c)
    steps.append(wait_step(site, head, c["port"], ["docker", "logs", "-f", "--tail", "20", NODE],
                           ["docker", "inspect", "-f", "{{.State.Running}}", NODE], "true"))


def boot_unit_steps(site, head, c):
    """A user unit on the head that re-forms the cluster after a reboot: the
    containers come back on their own (restart policy), the engine processes
    inside them do not."""
    unit = head.home(".config/systemd/user/" + BOOT_UNIT)
    rack = os.path.join(site.root, "rack")
    content = "\n".join([
        "[Unit]",
        "Description=dgx-serve: re-form %s across the rack after a reboot" % c["recipe"],
        "After=network-online.target docker.service",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=%s up %s --%s --boot" % (systemd_quote(rack), systemd_quote(c["recipe"]), c["platform"]),
        "RemainAfterExit=yes",
        "TimeoutStartSec=3600",
        "",
        "[Install]",
        "WantedBy=default.target",
        ""])
    return [step(head, "write", "the boot unit (needs linger: rack init says)", path=unit, content=content),
            run(head, ["systemctl", "--user", "daemon-reload"], "systemd reads it", ignore_errors=True),
            run(head, ["systemctl", "--user", "enable", BOOT_UNIT], "it runs at every boot", ignore_errors=True)]


def systemd_quote(a):
    a = str(a)
    if a and not any(ch in a for ch in ' \t"\'\\%$;'):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def gguf_path(site, artifact, revision):
    org, repo, path = artifact.split("/", 2)
    return os.path.join(hub_dir(site.hf_cache, org + "/" + repo), "snapshots", revision or "{revision}", path)


def plan_native(site, p, c, facts, runtime, replace):
    h = site.head
    v, args = c["v"], c["args"]
    steps = p["steps"]
    log = os.path.join(site.state, "logs", "engine.log")
    p["log"] = log
    uid = str(os.getuid())
    if runtime == "launchd":
        if replace:                                # stop rack's own engine, and only that, first
            steps += launchd_stop(h, uid)
        steps.append(step(h, "free", "nothing else serving here", launchd=LABEL, port=c["port"]))
    else:
        if replace:
            steps.append(run(h, ["systemctl", "--user", "stop", UNIT], "stop the engine rack started before",
                             ignore_errors=True, quiet=True))
        steps.append(step(h, "free", "nothing else serving here", systemd=UNIT, port=c["port"]))
    env = {}
    for kv in v.get("ENV_EXTRA", []):
        if kv and "=" in kv:
            k, val = kv.split("=", 1)
            env[k] = val
    if c["engine"] == "llamacpp":
        try:
            variant = engines.llamacpp_variant(facts)
        except engines.EngineError as e:
            raise PlanError(str(e))
        server = engines.llamacpp_server(site.state, variant)
        steps.append(step(h, "install", "llama.cpp %s (%s)" % (engines.LLAMACPP_BUILD, variant),
                          dest=engines.llamacpp_dir(site.state, variant), check=server,
                          assets=[{"url": engines.LLAMACPP_URL % n, "sha256": s} for n, s in engines.LLAMACPP_ASSETS[variant]]))
        artifact = v.get("ARTIFACT") or ""
        gguf = gguf_path(site, artifact, v.get("ARTIFACT_REVISION"))
        steps.append(step(h, "weights", "the GGUF file is here", artifact=artifact, file=gguf, cache=site.hf_cache,
                          recipe=c["recipe"], platform=c["platform"]))
        argv = [server, "-m", gguf, "--host", c["host"], "--port", str(c["port"]), "--alias", c["name"]]
        if site.key:
            argv += ["--api-key-file", site.key_file]
        mmproj = v.get("ARTIFACT_MMPROJ")
        if mmproj:
            argv += ["--mmproj", gguf_path(site, mmproj, v.get("ARTIFACT_REVISION"))]
        argv += strip_owned(args)
        libs = engines.llamacpp_libpath(site.state, variant)
        if libs:
            env["LD_LIBRARY_PATH"] = ":".join(libs)
        p["engine_build"] = "llama.cpp %s %s" % (engines.LLAMACPP_BUILD, variant)
    else:                                            # vLLM from a venv (Windows without Docker)
        venv = os.path.join(site.state, "engines", "vllm", engines.VLLM_VERSION)
        steps.append(step(h, "venv", "vLLM %s in a venv" % engines.VLLM_VERSION, dest=venv,
                          check=os.path.join(venv, "bin", "vllm"), package="vllm==%s" % engines.VLLM_VERSION))
        steps.append(step(h, "weights", "the weights are here", repo=c["model"], cache=site.hf_cache,
                          recipe=c["recipe"], platform=c["platform"]))
        argv = [os.path.join(venv, "bin", "vllm"), "serve", c["model"]] + strip_owned(args) + [
            "--host", c["host"], "--port", str(c["port"]), "--served-model-name", c["name"]]
        for kv in locality_env():
            k, val = kv.split("=", 1)
            env.setdefault(k, val)
        env["HF_HOME"] = site.hf_cache
        p["engine_build"] = "vLLM %s (venv)" % engines.VLLM_VERSION
    steps.append(run(h, ["mkdir", "-p", os.path.dirname(log)], "a place for the engine's log"))
    if runtime == "launchd":
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")
        content = plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},       # back after a crash, not after rack down
            "ThrottleInterval": 10, "ProcessType": "Interactive",
            "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": env}).decode()
        steps.append(step(h, "write", "the launchd job", path=plist, content=content, resolve_revision=True))
        steps += launchd_stop(h, uid)            # a job loaded but not running is still in the way
        steps.append(run(h, ["launchctl", "bootstrap", "gui/" + uid, plist], "start it, now and at every login"))
        alive = ["launchctl", "print", "gui/%s/%s" % (uid, LABEL)]
        steps.append(wait_step(site, h, c["port"], ["tail", "-n", "20", "-F", log], alive, None))
    else:
        unit = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", UNIT)
        lines = ["[Unit]", "Description=dgx-serve engine: %s (%s)" % (c["recipe"], p["engine_build"]),
                 "After=network-online.target", "", "[Service]", "Type=simple",
                 "ExecStart=" + " ".join(systemd_quote(a) for a in argv)]
        for k in sorted(env):
            lines.append("Environment=" + systemd_quote("%s=%s" % (k, env[k])))
        if c["engine"] == "vllm" and site.key:
            lines.append("EnvironmentFile=" + site.env_file)
        cap = p.get("memory_cap_gb")
        if cap:
            lines.append("MemoryMax=%dG" % cap)
        lines += ["Restart=on-failure", "RestartSec=5", "StandardOutput=append:" + log,
                  "StandardError=append:" + log, "", "[Install]", "WantedBy=default.target", ""]
        steps.append(step(h, "write", "the systemd user unit", path=unit, content="\n".join(lines),
                          resolve_revision=True))
        steps.append(run(h, ["systemctl", "--user", "daemon-reload"], "systemd reads it"))
        steps.append(run(h, ["systemctl", "--user", "enable", UNIT], "start it at every boot (with linger)"))
        steps.append(run(h, ["systemctl", "--user", "restart", UNIT], "start it now"))
        steps.append(wait_step(site, h, c["port"], ["tail", "-n", "20", "-F", log],
                               ["systemctl", "--user", "is-active", UNIT], "active"))


def launchd_stop(h, uid):
    """Unload the engine's job; bootout returns before it is gone, and
    bootstrapping over a job on its way out fails, so wait for it."""
    return [run(h, ["launchctl", "bootout", "gui/%s/%s" % (uid, LABEL)], "the job's previous run",
                ignore_errors=True, quiet=True),
            run(h, ["sh", "-c", "for i in 1 2 3 4 5 6 7 8 9 10; do launchctl print gui/%s/%s >/dev/null 2>&1 "
                                "|| exit 0; sleep 0.5; done" % (uid, LABEL)],
                "the previous run is gone", ignore_errors=True, quiet=True)]


def serving_record(site, p, c):
    d = c["d"]
    return {"schema": 1, "recipe": c["recipe"], "platform": c["platform"], "engine": c["engine"],
            "runtime": p["runtime"], "model": c["model"], "served_name": c["name"], "artifact": p.get("artifact"),
            "port": c["port"], "host": c["host"], "key_required": site.key, "nodes": [n["name"] for n in p["nodes"]],
            "roles": (c["v"].get("ROLES") or "").split(),
            "dialect": {k[len("DIALECT_"):].lower(): val for k, val in sorted(c["v"].items()) if k.startswith("DIALECT_")},
            "context": d.get("context"), "tools": d.get("tools"), "reasoning": d.get("reasoning"),
            "vision": d.get("vision"), "speculative": d.get("speculative"), "image": p.get("image"),
            "engine_build": p.get("engine_build"), "log": p.get("log"), "rack_version": site.version,
            "started_at": None}


# -------------------------------------------------------------------- down --
def plan_down(site):
    path = os.path.join(site.state, "serving.json")
    try:
        s = json.load(open(path))
    except (OSError, ValueError):
        s = None
    p = {"schema": 1, "command": "down", "serving": s, "steps": []}
    steps = p["steps"]
    names = {n.name: n for n in site.nodes}
    runtime = (s or {}).get("runtime")
    targets = [names[n] for n in (s or {}).get("nodes", []) if n in names] or site.nodes
    if runtime in (None, "docker"):
        for n in targets:
            steps.append(run(n, ["docker", "rm", "-f", SOLO, NODE] + site.foreign,
                             "stop serving on %s" % n.name, ignore_errors=True))
            steps.append(run(n, ["pkill", "-f", "[m]emwatch.sh serve_"], "its memory watchdog",
                             ignore_errors=True, quiet=True))
        if runtime == "docker" or os.path.exists(os.path.expanduser("~/.config/systemd/user/" + BOOT_UNIT)):
            steps.append(run(site.head, ["systemctl", "--user", "disable", BOOT_UNIT], "no re-forming at boot",
                             ignore_errors=True, quiet=True))
            steps.append(step(site.head, "remove", "the boot unit",
                              path=os.path.expanduser("~/.config/systemd/user/" + BOOT_UNIT)))
    if runtime in (None, "launchd") and sys.platform == "darwin":
        uid = str(os.getuid())
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")
        steps.append(run(site.head, ["launchctl", "bootout", "gui/%s/%s" % (uid, LABEL)], "stop the engine",
                         ignore_errors=True, quiet=runtime is None))
        steps.append(step(site.head, "remove", "the launchd job (no restart at login)", path=plist))
    if runtime in (None, "systemd") and sys.platform != "darwin":
        unit = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", UNIT)
        if runtime == "systemd" or os.path.exists(unit):
            steps.append(run(site.head, ["systemctl", "--user", "disable", "--now", UNIT], "stop the engine",
                             ignore_errors=True))
            steps.append(step(site.head, "remove", "the unit (no restart at boot)", path=unit))
    steps.append(step(site.head, "remove", "the record of what was serving", path=path))
    return p


# --------------------------------------------------------------- rendering --
def shell(argv, remote=False):
    def q(a):
        if remote and a.startswith("~/"):
            return '"$HOME"/' + shlex.quote(a[2:])
        return shlex.quote(a)
    return " ".join(q(a) for a in argv)


def render(p, out=sys.stdout):
    title = "plan: rack %s" % p["command"]
    if p["command"] == "up":
        title += " %s --%s" % (p["recipe"], p["platform"])
    out.write("\033[1m%s (nothing was run)\033[0m\n" % title)
    if p["command"] == "up":
        out.write("  %s with %s, %s; on %s\n" % (p["model"], p["engine"], p["runtime"],
                                                 ", ".join(n["name"] for n in p["nodes"])))
        out.write("  api        http://%s:%d/v1  (model %s%s)\n" % (p["host"], p["port"], p["served_name"],
                                                                    ", key required" if p["key_required"] else ""))
        if p.get("memory_cap_gb"):
            out.write("  memory     capped at %d GiB per node\n" % p["memory_cap_gb"])
        if p.get("assumed_facts"):
            out.write("  note       planned for a typical %s, not this machine\n" % p["platform"])
        for n in p.get("notes", []):
            out.write("  note       %s\n" % n)
    for i, s in enumerate(p["steps"], 1):
        where = s["node"]
        k = s["kind"]
        if k == "run":
            body = shell(s["argv"])
        elif k == "write":
            body = "write %s%s" % (("%s:" % s["container"]) if s.get("container") else "", s["path"])
        elif k == "spawn":
            body = "%s  > %s &" % (shell(s["argv"]), s["log"])
        elif k == "wait":
            body = "wait for %s, showing: %s" % (s["url"], shell(s["logs"]))
        elif k == "install":
            body = "install into %s: %s" % (s["dest"], ", ".join(a["url"].rsplit("/", 1)[-1] for a in s["assets"]))
        elif k == "weights":
            body = "check %s in %s (rack pull %s --%s if missing)" % (s.get("artifact") or s.get("repo"), s["cache"],
                                                                     s["recipe"], s["platform"])
        elif k == "free":
            body = "refuse if serving already: %s" % ", ".join(filter(None, [
                " ".join(s.get("containers") or []) and "containers %s" % " ".join(s.get("containers") or []),
                s.get("launchd") and "launchd %s" % s["launchd"], s.get("systemd") and "unit %s" % s["systemd"],
                s.get("port") and "port %s" % s["port"]]))
        elif k == "record":
            body = "write %s" % s["path"]
        elif k == "remove":
            body = "remove %s" % s["path"]
        else:
            body = ", ".join("%s=%s" % (a, b) for a, b in s.items() if a not in ("node", "kind", "what"))
        out.write("  %2d  %-12s %s\n        %s\n" % (i, where, s["what"], body))


# --------------------------------------------------------------- executing --
class Executor:
    def __init__(self, site, plan, out=sys.stdout):
        self.site, self.plan, self.out = site, plan, out
        self.nodes = {n.name: n for n in site.nodes}
        self.poll = float(os.environ.get("RACK_POLL") or 2)

    def say(self, msg):
        self.out.write(msg + "\n")
        self.out.flush()

    def node(self, s):
        return self.nodes.get(s["node"]) or self.site.head

    def sh(self, n, argv, stdin=None, check=False, capture=True):
        if n.local:
            cmd = argv
        else:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", n.ssh, shell(argv, remote=True)]
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=capture, text=True)
        except FileNotFoundError:
            r = subprocess.CompletedProcess(cmd, 127, "", "%s: command not found" % cmd[0])
        if check and r.returncode != 0:
            raise PlanError("%s on %s failed (exit %d): %s" % (shell(argv), n.name, r.returncode,
                                                            ((r.stderr or "") + (r.stdout or "")).strip()[-800:]))
        return r

    def execute(self):
        for s in self.plan["steps"]:
            if not s.get("quiet"):
                self.say("== %s%s" % (s["what"], "" if self.node(s).local else " (%s)" % s["node"]))
            getattr(self, "do_" + s["kind"].replace("-", "_"))(s)

    # ---- kinds
    def do_run(self, s):
        n = self.node(s)
        r = self.sh(n, s["argv"], capture=s.get("quiet", False) or s.get("fail") is not None)
        if r.returncode != 0 and not s.get("ignore_errors"):
            raise PlanError(s.get("fail") or "%s failed on %s (exit %d)" % (shell(s["argv"]), n.name, r.returncode))

    def do_free(self, s):
        n = self.node(s)
        busy = []
        if s.get("containers"):
            r = self.sh(n, ["docker", "ps", "--format", "{{.Names}}"])
            running = set((r.stdout or "").split())
            busy += sorted(running & set(s["containers"]))
            r = self.sh(n, ["docker", "ps", "--filter", "label=" + s["label"], "--format", "{{.Names}}"])
            busy += sorted(set((r.stdout or "").split()) - set(busy))
        if s.get("launchd"):
            r = self.sh(n, ["launchctl", "print", "gui/%d/%s" % (os.getuid(), s["launchd"])])
            if r.returncode == 0 and "state = running" in (r.stdout or ""):
                busy.append("launchd job %s" % s["launchd"])
        if s.get("systemd"):
            r = self.sh(n, ["systemctl", "--user", "is-active", s["systemd"]])
            if (r.stdout or "").strip() in ("active", "activating"):
                busy.append("unit %s" % s["systemd"])
        if s.get("port") and not busy and port_open(s["port"]):
            busy.append("something on port %s" % s["port"])
        if busy:
            raise PlanError("%s is already serving (%s): rack down first, or rack up --replace to swap rack's own engine"
                            % (n.name, ", ".join(busy)))

    def do_image(self, s):
        n = self.node(s)
        if self.sh(n, ["docker", "image", "inspect", s["image"]]).returncode == 0:
            return
        if s.get("build"):
            raise PlanError("image %s is missing: rack build (it also ships it to the workers)" % s["image"])
        self.say("   pulling %s (engine images are several GB)" % s["image"])
        if subprocess.run(["docker", "pull", s["image"]]).returncode != 0:
            raise PlanError("could not pull %s" % s["image"])

    def do_same_image(self, s):
        ids = {}
        for name in s["nodes"]:
            r = self.sh(self.nodes[name], ["docker", "image", "inspect", "--format", "{{.Id}}", s["image"]])
            ids[name] = (r.stdout or "").strip() if r.returncode == 0 else "(missing)"
        if len(set(ids.values())) != 1 or "(missing)" in ids.values():
            raise PlanError("the image differs between nodes (%s): rack build ships it to every worker"
                            % ", ".join("%s %s" % (k, v[:19]) for k, v in ids.items()))

    def do_weights(self, s):
        missing = [n for n in (s.get("nodes") or [s["node"]]) if not self.weights_ok(self.nodes.get(n, self.site.head), s)]
        if not missing:
            return
        if self.site.head.name in missing or not s.get("nodes"):
            self.say("   not here yet: rack pull %s --%s" % (s["recipe"], s["platform"]))
            rack = os.path.join(self.site.root, "rack")
            if subprocess.run([rack, "pull", s["recipe"], "--" + s["platform"]]).returncode != 0:
                raise PlanError("rack pull %s failed" % s["recipe"])
        elif s.get("repo"):
            if subprocess.run([os.path.join(self.site.root, "scripts", "sync-model.sh"), s["repo"]]).returncode != 0:
                raise PlanError("replicating %s to %s failed" % (s["repo"], ", ".join(missing)))
        still = [n for n in (s.get("nodes") or [s["node"]]) if not self.weights_ok(self.nodes.get(n, self.site.head), s)]
        if still:
            raise PlanError("the weights are still incomplete on %s: rack verify" % ", ".join(still))

    def weights_ok(self, n, s):
        if s.get("artifact"):
            return bool(resolve_gguf(s["file"], s["cache"], s["artifact"]))
        cache = n.hf_cache or s["cache"]
        code = ("import glob,json,os,sys\n"
                "g=sorted(glob.glob(os.path.join(sys.argv[1],'snapshots','*','')))\n"
                "ok=False\n"
                "for p in g:\n"
                " f=os.path.join(p,'model.safetensors.index.json')\n"
                " if os.path.exists(f):\n"
                "  w=set(json.load(open(f))['weight_map'].values()); ok=all(os.path.exists(p+x) for x in w)\n"
                " else:\n"
                "  ok=bool(glob.glob(p+'*.safetensors'))\n"
                " if ok: break\n"
                "sys.exit(0 if ok else 1)\n")
        return self.sh(n, ["python3", "-c", code, hub_dir(cache, s["repo"])]).returncode == 0

    def do_reach(self, s):
        deadline = time.time() + s["timeout"]
        for name in s["nodes"]:
            n = self.nodes[name]
            while self.sh(n, ["true"]).returncode != 0:
                if time.time() > deadline:
                    raise PlanError("%s does not answer over ssh" % name)
                time.sleep(self.poll * 5)

    def do_write(self, s):
        n = self.node(s)
        content = s["content"]
        if s.get("resolve_revision") and "{revision}" in content:
            content = resolve_revision_in(content, self.site.hf_cache)
        if s.get("container"):
            argv = ["docker", "exec", "-i", s["container"], "sh", "-c",
                    "cat > %s && chmod %s %s" % (s["path"], s.get("mode", "0644"), s["path"])]
            self.sh(n, argv, stdin=content, check=True)
        elif n.local:
            os.makedirs(os.path.dirname(s["path"]), exist_ok=True)
            tmp = s["path"] + ".tmp"
            with open(tmp, "w") as f:
                f.write(content)
            os.chmod(tmp, int(s.get("mode", "0644"), 8))
            os.replace(tmp, s["path"])
        else:
            d = os.path.dirname(s["path"])
            self.sh(n, ["sh", "-c", 'mkdir -p "$1" && cat > "$2" && chmod "$3" "$2"', "sh", d, s["path"],
                        s.get("mode", "0644")], stdin=content, check=True)

    def do_spawn(self, s):
        n = self.node(s)
        if n.local:
            os.makedirs(os.path.dirname(s["log"]), exist_ok=True)
            with open(s["log"], "ab") as log:
                subprocess.Popen(s["argv"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        else:
            inner = 'mkdir -p "$(dirname "$1")"; shift_log=$1; shift; nohup "$@" >> "$shift_log" 2>&1 < /dev/null &'
            self.sh(n, ["sh", "-c", inner, "sh", s["log"]] + s["argv"])

    def do_mod(self, s):
        n = self.node(s)
        dest = "/workspace/mods/%s" % s["name"]
        self.sh(n, ["docker", "exec", "-w", "/", s["container"], "mkdir", "-p", dest], check=True)
        tar = subprocess.run(["tar", "-C", s["src"], "-cf", "-", "."], capture_output=True).stdout
        cmd = ["docker", "exec", "-i", s["container"], "tar", "-C", dest, "-xf", "-"]
        if n.local:
            r = subprocess.run(cmd, input=tar)
        else:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", n.ssh, shell(cmd)], input=tar)
        if r.returncode != 0:
            raise PlanError("copying mod %s to %s failed" % (s["name"], n.name))
        self.sh(n, ["docker", "exec", s["container"], "bash", "-c",
                    "cd %s && chmod +x run.sh && ./run.sh" % shlex.quote(dest)], check=True)

    def do_install(self, s):
        if os.path.exists(s["check"]):
            return
        import fetch
        os.makedirs(s["dest"], exist_ok=True)
        for a in s["assets"]:
            name = a["url"].rsplit("/", 1)[-1]
            tar = os.path.join(s["dest"], name)
            fetch.download(a["url"], tar, sha256=a["sha256"], progress=fetch.progress_printer(name))
            engines.safe_extract(tar, s["dest"])
            os.remove(tar)
        if not os.path.exists(s["check"]):
            raise PlanError("installed, but %s is not there" % s["check"])

    def do_venv(self, s):
        if os.path.exists(s["check"]):
            return
        if subprocess.run([sys.executable, "-m", "venv", s["dest"]]).returncode != 0:
            raise PlanError("could not create a venv at %s (sudo apt install python3-venv)" % s["dest"])
        pip = os.path.join(s["dest"], "bin", "pip")
        if subprocess.run([pip, "install", "--upgrade", "pip"]).returncode != 0 or \
                subprocess.run([pip, "install", s["package"]]).returncode != 0:
            raise PlanError("installing %s into %s failed" % (s["package"], s["dest"]))

    def do_wait(self, s):
        logs = None
        if s.get("logs") and (sys.stdout.isatty() or os.environ.get("RACK_STREAM_LOGS") == "1"):
            logs = subprocess.Popen(s["logs"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    start_new_session=True)
            threading.Thread(target=self._pump, args=(logs,), daemon=True).start()
        deadline = time.time() + s["timeout"]
        last_alive = 0.0
        try:
            while True:
                if healthy(s["url"]):
                    self.say("== healthy: %s" % s["url"].rsplit("/", 1)[0] + "/v1")
                    return
                if s.get("alive") and time.time() - last_alive > self.poll * 3:
                    last_alive = time.time()
                    try:
                        r = subprocess.run(s["alive"], capture_output=True, text=True)
                    except FileNotFoundError:
                        r = subprocess.CompletedProcess(s["alive"], 127, "", "")
                    want = s.get("alive_value")
                    dead = r.returncode != 0 if want is None else (r.stdout or "").strip() not in (want, "activating")
                    if dead:
                        raise PlanError("the engine stopped while starting: rack logs")
                if time.time() > deadline:
                    raise PlanError("not healthy after %d s: rack logs -f (it keeps trying in the background)"
                                    % s["timeout"])
                time.sleep(self.poll)
        except KeyboardInterrupt:
            self.say("\n== detached: the engine keeps starting in the background (rack status, rack logs -f)")
            raise SystemExit(0)
        finally:
            if logs:
                logs.terminate()

    def _pump(self, proc):
        for line in proc.stdout:
            self.out.write("   " + line)
            self.out.flush()

    def do_record(self, s):
        rec = dict(s["content"])
        rec["started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(s["path"]), exist_ok=True)
        tmp = s["path"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, s["path"])

    def do_remove(self, s):
        try:
            os.remove(s["path"])
        except FileNotFoundError:
            pass


def port_open(port):
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def healthy(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def resolve_gguf(path, cache, artifact):
    if "{revision}" not in path:
        return path if os.path.exists(path) else None
    org, repo, _ = artifact.split("/", 2)
    try:
        rev = open(os.path.join(hub_dir(cache, org + "/" + repo), "refs", "main")).read().strip()
    except OSError:
        return None
    p = path.replace("{revision}", rev)
    return p if os.path.exists(p) else None


def resolve_revision_in(content, cache):
    """Fill {revision} with refs/main of whichever repo the path names."""
    import re
    def sub(m):
        repo_dir = m.group(1)
        try:
            return repo_dir + "/snapshots/" + open(os.path.join(repo_dir, "refs", "main")).read().strip()
        except OSError:
            return m.group(0)
    return re.sub(r"(/[^\s<\"]*?/models--[^/\s]+)/snapshots/\{revision\}", sub, content)


# --------------------------------------------------------------------- CLI --
def main(argv):
    ap = argparse.ArgumentParser(prog="serve.py")
    ap.add_argument("command", choices=["up", "down"])
    ap.add_argument("--recipe-name")
    ap.add_argument("--recipe-file")
    ap.add_argument("--recipe-dir")
    ap.add_argument("--platform")
    ap.add_argument("--facts")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--boot", action="store_true")
    a = ap.parse_args(argv)
    site = Site()
    try:
        if a.command == "up":
            facts = json.loads(a.facts or "{}")
            assumed = False
            if facts.get("platform") != a.platform:
                facts, assumed = dict(ASSUMED_FACTS[a.platform]), True
            p = plan_up(site, a.recipe_name, a.recipe_file, a.recipe_dir, a.platform, facts,
                        replace=a.replace, boot=a.boot, assumed=assumed)
        else:
            p = plan_down(site)
        if a.plan:
            if a.json:
                print(json.dumps(p, indent=None, separators=(",", ":")))
            else:
                render(p)
            return 0
        if a.command == "up" and p.get("notes"):
            for n in p["notes"]:
                print("note: " + n)
        Executor(site, p).execute()
        return 0
    except (PlanError, engines.EngineError, RuntimeError) as e:   # RuntimeError: a recipe that failed to source
        sys.stderr.write("\033[31m%s\033[0m\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
