"""Download a Hugging Face model repo, or files of one, into the hub cache.
Stdlib only; the same cache layout huggingface_hub writes, so vLLM and
llama.cpp find what this fetched (and hf download resumes nothing it did).

    python3 py/hfget.py <org/name> [--revision R] [--file PATH ...] [--weights]
        [--cache DIR] [--jobs N] [--reserve-gb G] [--dry-run] [--json]

  --weights   what vLLM loads: safetensors and their configs and tokenizer;
              not GGUF files, not original/ checkpoints, not .bin or .pth
              when safetensors exist
  --file      exactly these files (one GGUF file for llama.cpp)

The listing comes from /api/models/<repo>/revision/<rev>?blobs=true: every
file's size, and its sha256 (LFS) or git blob id. Each file downloads with
HTTP Range resume, is verified against that hash, and only then lands in
blobs/; snapshots/<commit>/<path> links to it and refs/<revision> names the
commit. The token comes from $HF_TOKEN, ~/.config/dgx-serve/hf-token or
~/.cache/huggingface/token, goes only to the hub (never to the CDN it
redirects to), and is never printed. $HF_ENDPOINT points elsewhere.
"""
import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402
import term  # noqa: E402

WEIGHT_FILES = ["*.safetensors", "*.json", "*.txt", "*.model", "*.tiktoken", "*.py", "*.jinja", "*.md",
                "tokenizer*", "*.vocab", "merges.txt", "*.spm"]
NEVER = ["original/*", "*/original/*", "*.gguf", "*.onnx", "*.onnx_data", "*.h5", "*.msgpack", "*.ot",
         "*.tflite", "*.mlmodel", "*.mlpackage/*", "consolidated*", ".gitattributes"]
FALLBACK_WEIGHTS = ["*.bin", "*.pt", "*.pth"]


class HubError(Exception):
    pass


def endpoint():
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


def token():
    t = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if t:
        return t.strip()
    config = os.environ.get("DGX_SERVE_CONFIG") or os.path.expanduser("~/.config/dgx-serve")
    for p in (os.path.join(config, "hf-token"), os.path.expanduser("~/.cache/huggingface/token")):
        try:
            t = open(p).read().strip()
            if t:
                return t
        except OSError:
            pass
    return None


def listing(repo, revision, tok):
    """(commit, [{path, size, sha256, blob}]) for a repo at a revision."""
    url = "%s/api/models/%s/revision/%s?blobs=true" % (endpoint(), repo, urllib.parse.quote(revision, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with fetch.opener().open(req, timeout=60) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HubError("%s is gated or private: accept its terms on the hub, then put a read token in "
                           "~/.config/dgx-serve/hf-token%s" % (repo, " (the token there was refused)" if tok else ""))
        if e.code == 404:
            raise HubError("%s@%s is not on the hub (check the name and the revision)" % (repo, revision))
        raise HubError("the hub answered HTTP %d for %s" % (e.code, repo))
    except (urllib.error.URLError, OSError) as e:
        raise HubError("cannot reach %s: %s" % (endpoint(), getattr(e, "reason", e)))
    files = []
    for s in d.get("siblings") or []:
        lfs = s.get("lfs") or {}
        files.append({"path": s["rfilename"], "size": s.get("size") if s.get("size") is not None else lfs.get("size"),
                      "sha256": lfs.get("sha256"), "blob": lfs.get("sha256") or s.get("blobId")})
    return d.get("sha"), files


def select(files, wanted=None, weights=False):
    def match(path, pats):
        return any(fnmatch.fnmatch(path, p) for p in pats)
    if wanted:
        by = {f["path"]: f for f in files}
        missing = [w for w in wanted if w not in by]
        if missing:
            raise HubError("not in the repo: %s" % ", ".join(missing))
        return [by[w] for w in wanted]
    out = [f for f in files if not match(f["path"], NEVER)]
    if weights:
        pats = list(WEIGHT_FILES)
        if not any(f["path"].endswith(".safetensors") for f in out):
            pats += FALLBACK_WEIGHTS            # an old repo with only .bin weights
        out = [f for f in out if match(f["path"], pats)]
    return out


def repo_dir(cache, repo):
    return os.path.join(cache, "hub", "models--" + repo.replace("/", "--"))


def git_blob_sha1(path):
    h = hashlib.sha1()
    h.update(b"blob %d\0" % os.path.getsize(path))
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Progress:
    def __init__(self, total, quiet=False):
        self.total, self.done, self.quiet = total, {}, quiet
        self.lock, self.t0, self.last = threading.Lock(), time.time(), 0.0

    def cb(self, name):
        def f(done, _total):
            with self.lock:
                self.done[name] = done
                self.report()
        return f

    def report(self, final=False):
        now = time.time()
        if self.quiet or (not final and now - self.last < 2.0):
            return
        self.last = now
        got = sum(self.done.values())
        rate = got / max(0.001, now - self.t0)
        pct = 100.0 * got / self.total if self.total else 100.0
        sys.stderr.write("  %s of %s  %5.1f%%  %s/s\n" % (fetch.human(got), fetch.human(self.total), pct, fetch.human(rate)))
        sys.stderr.flush()


def fetch_blob(repo, commit, f, rd, tok, progress):
    """One blob into blobs/, verified (sha256 for LFS files, the git blob id otherwise)."""
    blob = os.path.join(rd, "blobs", f["blob"])
    if os.path.exists(blob):
        progress.cb(f["path"])(f["size"] or 0, None)
        return
    url = "%s/%s/resolve/%s/%s" % (endpoint(), repo, commit, urllib.parse.quote(f["path"]))
    fetch.download(url, blob, sha256=f["sha256"], size=f["size"], token=tok, progress=progress.cb(f["path"]))
    if not f["sha256"] and git_blob_sha1(blob) != f["blob"]:
        os.remove(blob)
        raise HubError("%s: content does not match the repo's git blob %s" % (f["path"], f["blob"]))


def link_file(rd, commit, f):
    """snapshots/<commit>/<path> -> ../../blobs/<id>, as the hub cache does."""
    blob = os.path.join(rd, "blobs", f["blob"])
    link = os.path.join(rd, "snapshots", commit, f["path"])
    os.makedirs(os.path.dirname(link), exist_ok=True)
    target = os.path.relpath(blob, os.path.dirname(link))
    if os.path.lexists(link):
        if os.path.islink(link) and os.readlink(link) == target:
            return
        os.remove(link)
    try:
        os.symlink(target, link)
    except OSError:                               # a filesystem without symlinks
        shutil.copyfile(blob, link)


def pull(repo, revision="main", files=None, weights=False, cache=None, jobs=4, reserve_gb=2.0, dry_run=False,
         quiet=False):
    cache = cache or os.environ.get("HF_CACHE") or os.path.expanduser("~/.cache/huggingface")
    tok = token()
    commit, listed = listing(repo, revision, tok)
    if not commit:
        raise HubError("the hub returned no commit for %s@%s" % (repo, revision))
    chosen = select(listed, files, weights)
    if not chosen:
        raise HubError("nothing to download from %s (no files matched)" % repo)
    rd = repo_dir(cache, repo)
    need = 0
    for f in chosen:
        blob = os.path.join(rd, "blobs", f["blob"] or "")
        if not os.path.exists(blob):
            part = blob + ".part"
            need += (f["size"] or 0) - (os.path.getsize(part) if os.path.exists(part) else 0)
    os.makedirs(cache, exist_ok=True)
    free = shutil.disk_usage(cache).free
    total = sum(f["size"] or 0 for f in chosen)
    summary = {"repo": repo, "revision": revision, "commit": commit, "files": [f["path"] for f in chosen],
               "bytes": total, "to_download": need, "free": free, "cache": cache,
               "snapshot": os.path.join(rd, "snapshots", commit)}
    if dry_run:
        return summary
    if need + reserve_gb * 1e9 > free:
        raise HubError("%s needs %s more and %s is free at %s (with %.0f GB kept free): make room, or point HF_CACHE "
                       "at a bigger disk" % (repo, fetch.human(need), fetch.human(free), cache, reserve_gb))
    for d in ("blobs", "refs", os.path.join("snapshots", commit)):
        os.makedirs(os.path.join(rd, d), exist_ok=True)
    if not quiet:
        sys.stderr.write("  %s@%s: %d files, %s (%s to fetch)\n" % (repo, commit[:7], len(chosen), fetch.human(total),
                                                                    fetch.human(need)))
    progress = Progress(total, quiet)
    errors = []
    blobs = {}
    for f in chosen:                             # one download per blob, however many paths share it
        if not f["blob"]:
            raise HubError("%s: the hub listed no hash for it, so it cannot be verified" % f["path"])
        blobs.setdefault(f["blob"], f)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(fetch_blob, repo, commit, f, rd, tok, progress): f for f in blobs.values()}
        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except (fetch.FetchError, HubError, OSError) as e:
                errors.append("%s: %s" % (futs[fut]["path"], e))
    progress.report(final=True)
    if errors:
        raise HubError("; ".join(errors))
    for f in chosen:
        link_file(rd, commit, f)
    if not (len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)):
        with open(os.path.join(rd, "refs", revision), "w") as f:   # a branch or tag names a commit
            f.write(commit)
    return summary


def main(argv):
    ap = argparse.ArgumentParser(prog="hfget.py", description="Download from the Hugging Face hub into its cache.")
    ap.add_argument("repo")
    ap.add_argument("--revision", default="main")
    ap.add_argument("--file", action="append", dest="files")
    ap.add_argument("--weights", action="store_true")
    ap.add_argument("--cache")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--reserve-gb", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.repo.count("/") != 1:
        sys.stderr.write("a Hugging Face repo is org/name (got: %s)\n" % a.repo)
        return 2
    try:
        s = pull(a.repo, a.revision, a.files, a.weights, a.cache, a.jobs, a.reserve_gb, a.dry_run, a.quiet or a.json)
    except (HubError, fetch.FetchError) as e:
        sys.stderr.write(term.paint(e, "31", sys.stderr) + "\n")
        return 1
    if a.json:
        print(json.dumps(s))
    elif a.dry_run:
        print("  %s@%s  %d files, %s, %s to download, %s free at %s" % (
            s["repo"], s["commit"][:7], len(s["files"]), fetch.human(s["bytes"]), fetch.human(s["to_download"]),
            fetch.human(s["free"]), s["cache"]))
    else:
        print("  done: %s" % s["snapshot"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
