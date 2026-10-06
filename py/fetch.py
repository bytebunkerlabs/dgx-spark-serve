"""One file over HTTPS, resumably, checked against its sha256. Stdlib only.

Used for engine builds (llama.cpp release assets) and, through hfget.py, for
model weights. A partial download is kept as <dest>.part and resumed with an
HTTP Range request; the finished file is renamed into place only after its
sha256 matches, so a file at <dest> is always a whole, verified one.
"""
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request

CHUNK = 1 << 20
USER_AGENT = "dgx-serve/1.0 (+https://bytebunkerlabs.ai)"


class FetchError(Exception):
    pass


def sha256_file(path, upto=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            want = CHUNK if upto is None else min(CHUNK, upto - done)
            if want <= 0:
                break
            b = f.read(want)
            if not b:
                break
            h.update(b)
            done += len(b)
    return h.hexdigest()


class _NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the Authorization header to another
    host (the hub redirects LFS files to a CDN)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.request.urlparse(newurl).netloc != urllib.request.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def opener():
    return urllib.request.build_opener(_NoAuthOnRedirect)


def download(url, dest, sha256=None, size=None, token=None, progress=None, retries=4, timeout=60):
    """Fetch url to dest. Resumes dest.part; verifies sha256 when given.
    progress(done, total) is called as bytes arrive."""
    if os.path.exists(dest) and sha256 and sha256_file(dest) == sha256:
        return dest
    host = urllib.request.urlparse(url).hostname or ""
    if os.environ.get("RACK_OFFLINE") == "1" and host not in ("127.0.0.1", "localhost"):
        raise FetchError("%s: RACK_OFFLINE=1, not downloading" % url)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    part = dest + ".part"
    attempt = 0
    while True:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size is not None and have > size:
            os.remove(part)
            have = 0
        if size is not None and have == size:
            break
        headers = {"User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = "Bearer " + token
        if have:
            headers["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener().open(req, timeout=timeout) as r:
                status = r.status
                if have and status == 200:          # no Range support: start over
                    have = 0
                total = None
                cl = r.headers.get("Content-Length")
                if cl is not None:
                    total = have + int(cl) if status == 206 else int(cl)
                total = size if size is not None else total
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    while True:
                        b = r.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
                        done += len(b)
                        if progress:
                            progress(done, total)
            if size is None or os.path.getsize(part) >= size:
                break
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:              # we already have it all
                break
            if e.code in (401, 403):
                raise FetchError("%s: HTTP %d (a gated repo needs a Hugging Face token: "
                                 "~/.config/dgx-serve/hf-token)" % (url, e.code))
            if e.code == 404:
                raise FetchError("%s: not found (HTTP 404)" % url)
            if attempt >= retries:
                raise FetchError("%s: HTTP %d" % (url, e.code))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt >= retries:
                raise FetchError("%s: %s" % (url, getattr(e, "reason", e)))
        attempt += 1
        time.sleep(min(30, 2 ** attempt))
    if size is not None and os.path.getsize(part) != size:
        raise FetchError("%s: got %d bytes, expected %d" % (url, os.path.getsize(part), size))
    if sha256:
        got = sha256_file(part)
        if got != sha256:
            os.remove(part)
            raise FetchError("%s: sha256 %s, expected %s (the partial file was removed)" % (url, got, sha256))
    os.replace(part, dest)
    return dest


def progress_printer(label, stream=sys.stderr, every=2.0):
    """A progress callback that prints a line every `every` seconds."""
    state = {"t": 0.0}

    def cb(done, total):
        now = time.time()
        if now - state["t"] < every and not (total and done >= total):
            return
        state["t"] = now
        if total:
            stream.write("  %s  %5.1f%%  %s of %s\n" % (label, 100.0 * done / total, human(done), human(total)))
        else:
            stream.write("  %s  %s\n" % (label, human(done)))
        stream.flush()
    return cb


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1000.0
