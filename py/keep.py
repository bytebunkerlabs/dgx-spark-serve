"""rack keep: bring a dead engine back. Stdlib only.

    python3 py/keep.py check --rack PATH     what cron runs every 2 minutes
    python3 py/keep.py on --rack PATH | off | status

A cluster's containers run `sleep infinity` and the engine is started inside
them, so an engine that dies leaves its container "Up" and Docker's restart
policy never fires (measured: GLM-5.3 dead for three days in a running
container after a worker's RPC timed out). A boot unit needs linger. This
needs neither: cron runs without a login, asks the engine's /health, and when
something is recorded as serving (serving.json) but has not answered for
KEEP_GRACE_MIN minutes (default 15: a big model loads in about 11), runs
`rack up <recipe> --replace`. At most once per KEEP_GAP_MIN (30) and
KEEP_MAX_PER_DAY (3) times a day, then it stops trying and says so. It does
nothing while a rack up or rack down runs, and nothing after rack down (which
removes serving.json). Every decision that changes something goes to
logs/keep.log; its state is keep.json, both in DGX_SERVE_STATE.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BEGIN, END = "# >>> dgx-serve keep", "# <<< dgx-serve keep"
DAY = 86400


def decide(state, now, serving, healthy, busy, grace_s=900, gap_s=1800, max_per_day=3, lost=(), lost_grace_s=100):
    """(action, new state, why): action is none, restart or giveup. Pure. `lost`: the nodes whose
    share of a cluster's engine is gone; the head's /health still answers then (it did for 7.5 hours
    after spark-2 rebooted on 2026-10-18), so a lost share is served again after two checks, not 15 min."""
    st = dict(state or {})
    st["restarts"] = [t for t in st.get("restarts") or [] if now - t < DAY]
    if not serving:
        return "none", {"restarts": st["restarts"]}, "nothing is recorded as serving"
    if busy:
        return "none", st, "a rack up or rack down is running"
    if healthy and not lost:
        st.pop("first_bad", None)
        st.pop("gave_up", None)
        return "none", st, "the engine answers"
    if st.get("recipe") != serving:
        st = {"restarts": st["restarts"], "recipe": serving}
    first = st.setdefault("first_bad", now)
    wait = lost_grace_s if lost else grace_s
    what = ("%s's share of the engine is gone" % ", ".join(lost)) if lost else "not answering"
    if now - first < wait:
        return "none", st, "%s for %d s (waits %d s%s)" % (what, now - first, wait, "" if lost else ": it may be loading")
    last = max(st["restarts"]) if st["restarts"] else None
    if last is not None and now - last < gap_s:
        return "none", st, "restarted %d s ago: gives it %d s" % (now - last, gap_s)
    if len(st["restarts"]) >= max_per_day:
        if st.get("gave_up"):
            return "none", st, "gave up already today"
        st["gave_up"] = now
        return "giveup", st, "%d restarts in a day did not bring it back: look at rack logs" % len(st["restarts"])
    st["restarts"].append(now)
    st["first_bad"] = now                       # the restart's own load gets the grace again
    return "restart", st, "%s for %d s" % (what, now - first)


def with_block(crontab, line):
    """crontab text with our block (BEGIN..END) set to `line`, or removed when line is None."""
    out, inside = [], False
    for ln in (crontab or "").splitlines():
        if ln.strip() == BEGIN:
            inside = True
            continue
        if ln.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    if line:
        out += ([""] if out else []) + [BEGIN, line, END]
    return "\n".join(out) + "\n" if out else ""


def cron_line(rack):
    home = os.path.expanduser("~")
    path = ":".join([os.path.join(home, ".local", "bin"), "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                     "/usr/bin", "/sbin", "/bin"])
    return "*/2 * * * * PATH=%s %s keep >/dev/null 2>&1" % (path, shlex.quote(rack))


def crontab_text():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def set_crontab(text):
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


class Keep:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "keep.json")
        self.log_path = os.path.join(state_dir, "logs", "keep.log")

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, st):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.path)

    def log(self, msg):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))

    def serving(self):
        try:
            with open(os.path.join(self.state_dir, "serving.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def nodes():
    """[(name, ssh)] of the rack, the head first with no ssh, from RACK_NODES (rack passes it)."""
    out = []
    for line in (os.environ.get("RACK_NODES") or "").splitlines():
        f = line.split("\t")
        if f and f[0]:
            out.append((f[0], f[1] if len(f) > 1 else ""))
    return out


def on_node(ssh, command, timeout=30):
    """(exit code, output) of a command on a node: here when ssh is empty, else over ssh.
    The code is None when the node cannot be asked (ssh failed)."""
    argv = (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh, command] if ssh
            else ["sh", "-c", command])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return (None if ssh and r.returncode == 255 else r.returncode), r.stdout


def lost_shares(serving, rack_nodes, container):
    """The nodes whose share of a cluster's engine is not running in its container. A node that
    cannot be asked is not counted lost: serving it again could not form the cluster anyway."""
    names = (serving or {}).get("nodes") or []
    if (serving or {}).get("runtime") != "docker" or len(names) < 2:
        return []
    ssh = dict(rack_nodes)
    lost = []
    for n in names:
        if n not in ssh:
            continue
        code, out = on_node(ssh[n], "docker top %s 2>/dev/null" % container)
        if code is not None and "vllm" not in out:
            lost.append(n)
    return lost


def start_monitors(rack_nodes):
    """Start a node's monitor containers that exist but are stopped (Docker does not retry one that
    failed to start at boot: spark-2's stayed down for four days after 2026-10-18). Returns what started."""
    started = []
    for name, ssh in rack_nodes:
        code, out = on_node(ssh, "docker ps -a --filter name=^rack-monitor --format '{{.Names}} {{.State}}' 2>/dev/null")
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("rack-monitor", "rack-monitor-docker") and parts[1] != "running":
                c, _ = on_node(ssh, "docker start %s >/dev/null 2>&1" % parts[0])
                started.append("%s on %s%s" % (parts[0], name, "" if c == 0 else " (did not start)"))
    return started


def busy():
    """A rack up or rack down running now (the planner, py/serve.py)."""
    r = subprocess.run(["pgrep", "-f", r"py/serve\.py (up|down)"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def check(args):
    import fcntl
    import serve
    k = Keep(args.state)
    os.makedirs(args.state, exist_ok=True)
    lock = open(os.path.join(args.state, "keep.lock"), "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                                # the last check is still serving it again

    rack_nodes = nodes()
    for what in start_monitors(rack_nodes):
        k.log("monitor: started %s" % what)
    s = k.serving()
    recipe = (s or {}).get("recipe") or ""
    up = bool(s) and serve.healthy("http://127.0.0.1:%d/health" % int(s.get("port") or 8888))
    working = busy()
    lost = lost_shares(s, rack_nodes, serve.NODE) if s and up and not working else []
    action, st, why = decide(k.load(), int(time.time()), recipe, up, working, grace_s=args.grace * 60,
                             gap_s=args.gap * 60, max_per_day=args.max_per_day, lost=lost)
    k.save(st)
    if action == "giveup":
        k.log("%s: %s" % (recipe, why))
    elif action == "restart":
        k.log("%s: %s: rack up %s --replace" % (recipe, why, recipe))
        argv = [args.rack, "up", recipe, "--replace"] + (["--" + s["platform"]] if s.get("platform") else [])
        with open(k.log_path, "a") as out:
            code = subprocess.call(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        k.log("%s: rack up exited %d" % (recipe, code))
    if args.verbose:
        print("%s: %s" % (action, why))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="rack keep")
    p.add_argument("cmd", nargs="?", default="check", choices=("check", "on", "off", "status"))
    p.add_argument("--rack", default="rack")
    p.add_argument("--state", default=os.environ.get("DGX_SERVE_STATE")
                   or os.path.expanduser("~/.local/state/dgx-serve"))
    p.add_argument("--grace", type=int, default=int(os.environ.get("KEEP_GRACE_MIN") or 15))
    p.add_argument("--gap", type=int, default=int(os.environ.get("KEEP_GAP_MIN") or 30))
    p.add_argument("--max-per-day", type=int, default=int(os.environ.get("KEEP_MAX_PER_DAY") or 3))
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "check":
        return check(a)
    cur = crontab_text()
    if a.cmd == "on":
        set_crontab(with_block(cur, cron_line(os.path.abspath(a.rack))))
        print("keep: on (cron asks the engine every 2 minutes; a dead one is served again after %d minutes)" % a.grace)
    elif a.cmd == "off":
        if BEGIN in cur:
            set_crontab(with_block(cur, None))
        print("keep: off")
    else:
        k = Keep(a.state)
        st = k.load()
        print("keep: %s" % ("on" if BEGIN in cur else "off"))
        if st.get("first_bad"):
            print("  not answering since %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(st["first_bad"])))
        if st.get("restarts"):
            print("  restarts in the last day: %d" % len(st["restarts"]))
        if os.path.exists(k.log_path):
            with open(k.log_path) as f:
                tail = f.read().splitlines()[-5:]
            for ln in tail:
                print("  " + ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
