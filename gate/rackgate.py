#!/usr/bin/env python3
"""rack gate: the rack's own front door (docs/14-gate.md). Stdlib only.

One address and a key per client for every model the rack serves, in the
OpenAI API and in Anthropic's Messages API. Routes come from what is serving
(the head's serving.json) and gate.json; a model is listed only while its
engine names it. No admin endpoints, no database, no plugins, no code
execution: the only outbound calls go to the engines in its routes.

    python3 rackgate.py serve        (the container's command)

Environment: GATE_BIND (comma-separated addresses, default 127.0.0.1),
GATE_PORT (4001), GATE_SERVING (serving.json), GATE_CONFIG (gate.json),
GATE_KEYS (keys.json), GATE_ENGINE_KEY (the engine's key file), GATE_STATE
(the usage folder).
"""
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
MAX_BODY = 64 * 1024 * 1024          # images ride in the body
LIVE_EVERY = 10.0                    # ask each engine for its models at most this often
UPSTREAM_TIMEOUT = 600               # per read: a long think between tokens is fine
ANTHROPIC_ERRORS = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                    404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
                    500: "api_error", 502: "api_error", 503: "overloaded_error", 504: "api_error"}


def env(name, default=""):
    return os.environ.get(name) or default


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read_key(path):
    """A key file: the raw key, or NAME=key on its first line."""
    t = read_text(path).strip().splitlines()
    if not t:
        return ""
    line = t[0].strip()
    return line.split("=", 1)[1].strip().strip("'\"") if re.match(r"^[A-Z_]+=", line) else line


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- keys --
class Keys:
    """keys.json: {"clients": [{"name", "sha256", "models": ["*"], "created"}]}, re-read when it changes."""

    def __init__(self, path):
        self.path = path
        self.stamp = None
        self.by_hash = {}
        self.lock = threading.Lock()

    def _load(self):
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return
        by = {}
        try:
            for c in json.loads(read_text(self.path) or "{}").get("clients") or []:
                if c.get("sha256") and c.get("name"):
                    by[c["sha256"]] = {"name": c["name"], "models": c.get("models") or ["*"]}
        except ValueError:
            by = self.by_hash                     # a half-written file: keep what worked
        self.by_hash, self.stamp = by, stamp

    def client(self, key):
        if not key:
            return None
        with self.lock:
            self._load()
            return self.by_hash.get(sha256(key))


def allowed(client, model):
    return any(fnmatch.fnmatchcase(model, pat) for pat in client.get("models") or ["*"])


# ----------------------------------------------------------------- routes --
class Route:
    def __init__(self, name, url, model, key="", thinking="", context=0):
        self.name, self.url, self.model, self.key, self.thinking = name, url.rstrip("/"), model, key, thinking
        self.context = int(context or 0)       # the engine's window, when known (serving.json, gate.json)

    def root(self):
        """The engine's address without /v1 (vLLM's /tokenize lives there)."""
        return re.sub(r"/v1$", "", self.url)


class Routes:
    def __init__(self, serving_path, config_path, engine_key_path):
        self.serving_path, self.config_path, self.engine_key_path = serving_path, config_path, engine_key_path
        self.live = {}                       # engine url -> (time asked, set of model ids)
        self.lock = threading.Lock()

    def config(self):
        try:
            return json.loads(read_text(self.config_path) or "{}")
        except ValueError:
            return {}

    def all(self):
        """Every route, by public name: the serving recipe, then gate.json's."""
        out = {}
        try:
            s = json.loads(read_text(self.serving_path) or "null")
        except ValueError:
            s = None
        if s and s.get("served_name") and s.get("port"):
            name = s.get("gateway_name") or s["served_name"]
            key = read_key(self.engine_key_path) if s.get("key_required") else ""
            out[name] = Route(name, "http://127.0.0.1:%d/v1" % int(s["port"]), s["served_name"], key,
                              (s.get("dialect") or {}).get("thinking") or "", s.get("context") or 0)
        for r in self.config().get("routes") or []:
            if r.get("name") and r.get("url"):
                key = read_key(r["key_file"]) if r.get("key_file") else (r.get("key") or "")
                out[r["name"]] = Route(r["name"], r["url"], r.get("model") or r["name"], key, r.get("thinking") or "",
                                       r.get("context") or 0)
        return out

    def engine_models(self, route):
        """What the engine says it serves now (cached LIVE_EVERY seconds); empty when it does not answer."""
        now = time.time()
        with self.lock:
            hit = self.live.get((route.url, route.key))
            if hit and now - hit[0] < LIVE_EVERY:
                return hit[1]
        ids = set()
        try:
            req = urllib.request.Request(route.url + "/models", headers=auth_headers(route))
            with urllib.request.urlopen(req, timeout=4) as r:
                ids = {m.get("id") for m in json.loads(r.read().decode("utf-8")).get("data") or []}
        except Exception:   # noqa: BLE001 (not answering: not listed)
            pass
        with self.lock:
            self.live[(route.url, route.key)] = (now, ids)
        return ids

    def listed(self):
        return [r for r in self.all().values() if r.model in self.engine_models(r)]

    def resolve(self, name):
        """The route for a requested name: a route's own name, else an alias (exact, then a glob)."""
        routes = self.all()
        if name in routes:
            return routes[name]
        aliases = self.config().get("aliases") or {}
        target = aliases.get(name)
        if target is None:
            target = next((t for pat, t in aliases.items() if fnmatch.fnmatchcase(name, pat)), None)
        return routes.get(target) if target else None


def auth_headers(route):
    h = {"Content-Type": "application/json"}
    if route.key:
        h["Authorization"] = "Bearer " + route.key
    return h


def set_path(body, dotted, value):
    keys = [k for k in dotted.split(".") if k]
    node = body
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


# --------------------------------------------- Anthropic Messages <-> OpenAI --
def blocks_of(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def text_of(content):
    return "\n\n".join(b.get("text") or "" for b in blocks_of(content) if b.get("type") == "text")


def image_part(b):
    src = b.get("source") or {}
    if src.get("type") == "base64":
        url = "data:%s;base64,%s" % (src.get("media_type") or "image/png", src.get("data") or "")
    else:
        url = src.get("url") or ""
    return {"type": "image_url", "image_url": {"url": url}}


def tool_result_text(b):
    c = b.get("content")
    t = c if isinstance(c, str) else "\n".join(x.get("text") or "" for x in blocks_of(c) if x.get("type") == "text")
    return ("[error] " + t) if b.get("is_error") else t


def to_openai(req, route):
    """An Anthropic Messages request as a chat completions request for this route's engine."""
    msgs = []
    system = req.get("system")
    if system:
        msgs.append({"role": "system", "content": system if isinstance(system, str) else text_of(system)})
    for m in req.get("messages") or []:
        role = m.get("role")
        blocks = blocks_of(m.get("content"))
        if role == "assistant":
            text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id") or "toolu_" + uuid.uuid4().hex[:20], "type": "function",
                      "function": {"name": b.get("name") or "", "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            out = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            msgs.append(out)
            continue
        # a user turn: its tool results first (they answer the assistant's calls), then what it says
        for b in blocks:
            if b.get("type") == "tool_result":
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id") or "", "content": tool_result_text(b)})
        parts = []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                parts.append({"type": "text", "text": b.get("text") or ""})
            elif t == "image":
                parts.append(image_part(b))
            elif t == "document":
                src = b.get("source") or {}
                if src.get("type") == "text":
                    parts.append({"type": "text", "text": src.get("data") or ""})
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            msgs.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts) if only_text else parts})
    body = {"model": route.model, "messages": msgs, "max_tokens": req.get("max_tokens") or 4096}
    for a, o in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("stop_sequences", "stop")):
        if req.get(a) is not None:
            body[o] = req[a]
    tools = [{"type": "function", "function": {"name": t.get("name") or "", "description": t.get("description") or "",
                                                "parameters": t.get("input_schema") or {"type": "object"}}}
             for t in req.get("tools") or [] if t.get("name") and t.get("input_schema") is not None]
    if tools:
        body["tools"] = tools
        tc = req.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            body["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif kind == "none":
            body["tool_choice"] = "none"
        if tc.get("disable_parallel_tool_use"):
            body["parallel_tool_calls"] = False
    thinking = (req.get("thinking") or {}).get("type")
    if route.thinking and thinking in ("enabled", "disabled", "adaptive"):
        set_path(body, route.thinking, thinking != "disabled")
    if req.get("stream"):
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def stop_reason(finish, matched, stops):
    if finish == "length":
        return "max_tokens", None
    if finish == "tool_calls":
        return "tool_use", None
    if isinstance(matched, str) and matched in (stops or []):
        return "stop_sequence", matched
    return "end_turn", None


def reasoning_of(d):
    return d.get("reasoning_content") or d.get("reasoning") or ""


def from_openai(resp, model, stops):
    """A chat completions reply as an Anthropic message."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if reasoning_of(msg):
        content.append({"type": "thinking", "thinking": reasoning_of(msg), "signature": ""})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments") or ""}
        content.append({"type": "tool_use", "id": c.get("id") or "toolu_" + uuid.uuid4().hex[:20],
                        "name": fn.get("name") or "", "input": args})
    reason, seq = stop_reason(choice.get("finish_reason"), choice.get("stop_reason"), stops)
    if reason == "end_turn" and any(b["type"] == "tool_use" for b in content):
        reason = "tool_use"
    u = resp.get("usage") or {}
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": reason, "stop_sequence": seq,
            "usage": {"input_tokens": u.get("prompt_tokens") or 0, "output_tokens": u.get("completion_tokens") or 0}}


class StreamTranslator:
    """Chat completions stream chunks in, Anthropic Messages stream events out (as (event, data) pairs)."""

    def __init__(self, model, stops):
        self.model, self.stops = model, stops
        self.index = -1                  # the open content block's index
        self.kind = None                 # its type: thinking, text or tool_use
        self.tools = {}                  # the engine's tool index -> our block index
        self.finish = self.matched = None
        self.usage = {}
        self.started = False
        self.used_tools = False

    def _start(self):
        self.started = True
        return [("message_start", {"type": "message_start", "message": {
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})]

    def _close(self):
        if self.kind is None:
            return []
        out = []
        if self.kind == "thinking":
            out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                "delta": {"type": "signature_delta", "signature": ""}}))
        out.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        self.kind = None
        return out

    def _open(self, kind, block):
        out = self._close()
        self.index += 1
        self.kind = kind
        out.append(("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block}))
        return out

    def feed(self, chunk):
        out = [] if self.started else self._start()
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            r = reasoning_of(d)
            if r:
                if self.kind != "thinking":
                    out += self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
                out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "thinking_delta", "thinking": r}}))
            if d.get("content"):
                if self.kind != "text":
                    out += self._open("text", {"type": "text", "text": ""})
                out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "text_delta", "text": d["content"]}}))
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", 0)
                fn = tc.get("function") or {}
                if i not in self.tools:
                    self.used_tools = True
                    out += self._open("tool_use", {"type": "tool_use", "id": tc.get("id") or "toolu_" + uuid.uuid4().hex[:20],
                                                   "name": fn.get("name") or "", "input": {}})
                    self.tools[i] = self.index
                if fn.get("arguments"):
                    out.append(("content_block_delta", {"type": "content_block_delta", "index": self.tools[i],
                                                        "delta": {"type": "input_json_delta",
                                                                  "partial_json": fn["arguments"]}}))
            if ch.get("finish_reason"):
                self.finish, self.matched = ch["finish_reason"], ch.get("stop_reason")
        return out

    def end(self):
        out = [] if self.started else self._start()
        out += self._close()
        reason, seq = stop_reason(self.finish, self.matched, self.stops)
        if reason == "end_turn" and self.used_tools:
            reason = "tool_use"
        out.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": seq},
                                      "usage": {"input_tokens": self.usage.get("prompt_tokens") or 0,
                                                "output_tokens": self.usage.get("completion_tokens") or 0}}))
        out.append(("message_stop", {"type": "message_stop"}))
        return out


def sse_events(resp):
    """The data objects of an upstream SSE stream, until [DONE] or its end."""
    while True:
        line = resp.readline()
        if not line:
            return
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            return
        try:
            yield json.loads(data.decode("utf-8"))
        except ValueError:
            continue


WINDOW = re.compile(r"maximum context length is (\d+) tokens.*?prompt contains at least (\d+) input tokens", re.S)


def fit_max_tokens(body, route):
    """Claude clients ask for answers up to Claude's own windows (Claude Code: 32k to 128k): cap the answer
    so prompt and answer fit this engine's window, from a generous estimate of the prompt (3 chars a token)."""
    if not route.context:
        return
    prompt = len(json.dumps(body.get("messages") or [])) // 3 + len(json.dumps(body.get("tools") or [])) // 3
    room = max(256, route.context - prompt - 256)
    if body.get("max_tokens", 0) > room:
        body["max_tokens"] = room


def refit_after(err_text, body):
    """The engine's own numbers from a window refusal: the answer gets what is left, once. False when it cannot."""
    m = WINDOW.search(err_text or "")
    if not m:
        return False
    room = int(m.group(1)) - int(m.group(2)) - 16
    if room < 1 or room >= body.get("max_tokens", 0):
        return False
    body["max_tokens"] = room
    return True


def estimate_tokens(req):
    """A count for count_tokens when the engine cannot tokenize: about four characters a token."""
    n = len(json.dumps(req.get("system") or "")) + len(json.dumps(req.get("messages") or []))
    n += len(json.dumps(req.get("tools") or [])) if req.get("tools") else 0
    return max(1, n // 4)


# ------------------------------------------------------------------- usage --
class Usage:
    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()

    def record(self, **row):
        if not self.folder:
            return
        row = dict({"ts": round(time.time(), 3)}, **row)
        path = os.path.join(self.folder, "usage-%s.jsonl" % time.strftime("%Y-%m-%d"))
        try:
            with self.lock:
                os.makedirs(self.folder, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        except OSError:
            pass


# ----------------------------------------------------------------- server --
class Gate:
    def __init__(self):
        self.keys = Keys(env("GATE_KEYS", "/run/gate/keys.json"))
        self.routes = Routes(env("GATE_SERVING", "/run/dgx-serve/serving.json"), env("GATE_CONFIG", "/run/gate/gate.json"),
                             env("GATE_ENGINE_KEY", "/run/secrets/engine.key"))
        self.usage = Usage(env("GATE_STATE", "/run/gate-state"))


GATE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "rack-gate/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *args):
        pass                                  # requests go to the usage log, never prompts to a log

    # ---- helpers
    def anthropic_client(self):
        return bool(self.headers.get("anthropic-version") or self.headers.get("x-api-key"))

    def send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.sent = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, message, anthropic=False):
        if getattr(self, "sent", False):
            return                            # a stream already began: it just ends
        if anthropic:
            self.send_json(code, {"type": "error", "error": {"type": ANTHROPIC_ERRORS.get(code, "api_error"),
                                                             "message": message}})
        else:
            self.send_json(code, {"error": {"message": message, "type": "invalid_request_error" if code < 500
                                            else "server_error", "code": code}})

    def client(self):
        auth = self.headers.get("Authorization") or ""
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else (self.headers.get("x-api-key") or "").strip()
        return GATE.keys.client(key)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return None

    def upstream(self, route, path, payload):
        req = urllib.request.Request(route.url + path, data=json.dumps(payload).encode("utf-8"),
                                     headers=auth_headers(route), method="POST")
        return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)

    def stream_head(self, ctype="text/event-stream"):
        self.sent = True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # ---- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"ok": True, "version": VERSION})
            return
        anth = self.anthropic_client()
        if path == "/v1/models":
            c = self.client()
            if not c:
                self.error(401, "a gate key is needed (Authorization: Bearer, or x-api-key)", anth)
                return
            rows = [r for r in GATE.routes.listed() if allowed(c, r.name)]
            if anth:
                data = [{"type": "model", "id": r.name, "display_name": r.name, "created_at": "1970-01-01T00:00:00Z"}
                        for r in rows]
                self.send_json(200, {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                                     "last_id": data[-1]["id"] if data else None})
            else:
                self.send_json(200, {"object": "list", "data": [{"id": r.name, "object": "model", "created": 0,
                                                                 "owned_by": "rack"} for r in rows]})
            return
        self.error(404, "not found", anth)

    # ---- POST
    def do_POST(self):
        self.sent = False
        path = urllib.parse.urlparse(self.path).path
        anth = path.startswith("/v1/messages")
        if path not in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/messages",
                        "/v1/messages/count_tokens"):
            self.error(404, "not found", anth)
            return
        c = self.client()
        if not c:
            self.error(401, "a gate key is needed (Authorization: Bearer, or x-api-key)", anth)
            return
        req = self.body()
        if not isinstance(req, dict):
            self.error(400, "the body must be one JSON object (at most 64 MB)", anth)
            return
        name = str(req.get("model") or "")
        route = GATE.routes.resolve(name)
        if not route or not allowed(c, route.name):
            self.error(404, "no model %r here: GET /v1/models lists what this key may use" % name, anth)
            return
        started = time.time()
        row = {"client": c["name"], "model": route.name, "asked": name, "api": "anthropic" if anth else "openai",
               "path": path}
        try:
            if path == "/v1/messages":
                self.messages(req, route, name, row)
            elif path == "/v1/messages/count_tokens":
                self.count_tokens(req, route)
            else:
                self.passthrough(path[3:], req, route, row)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:600]
            e.close()
            row["status"] = e.code
            self.error(e.code if e.code in ANTHROPIC_ERRORS else 502, "the engine said: " + msg, anth)
        except (urllib.error.URLError, OSError) as e:
            if isinstance(e, (BrokenPipeError, ConnectionResetError)):
                row["status"] = 499                  # the client went away: stop reading the engine
            else:
                row["status"] = 502
                try:
                    self.error(502, "the engine did not answer: %s" % getattr(e, "reason", e), anth)
                except OSError:
                    pass
        finally:
            row.setdefault("status", 200)
            row["ms"] = int((time.time() - started) * 1000)
            if path != "/v1/messages/count_tokens":
                GATE.usage.record(**row)

    def passthrough(self, sub, req, route, row):
        """OpenAI API: the model renamed, the engine's key added, the reply relayed as it comes."""
        req["model"] = route.model
        resp = self.upstream(route, sub, req)
        with resp:
            if req.get("stream"):
                self.stream_head()
                last = b""
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    if line.startswith(b"data: {") and b'"usage"' in line:
                        last = line
                    if line == b"\n":
                        self.wfile.flush()
                if last:
                    try:
                        u = json.loads(last[6:].decode("utf-8")).get("usage") or {}
                        row.update(input=u.get("prompt_tokens"), output=u.get("completion_tokens"))
                    except ValueError:
                        pass
                return
            raw = resp.read()
            try:
                u = json.loads(raw.decode("utf-8")).get("usage") or {}
                row.update(input=u.get("prompt_tokens"), output=u.get("completion_tokens"))
            except ValueError:
                pass
            self.sent = True
            self.send_response(200)
            self.send_header("Content-Type", resp.headers.get("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def messages(self, req, route, asked, row):
        """Anthropic Messages, through the engine's chat completions."""
        body = to_openai(req, route)
        fit_max_tokens(body, route)
        stops = req.get("stop_sequences") or []
        try:
            resp = self.upstream(route, "/chat/completions", body)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            e.close()
            if e.code != 400 or not refit_after(text, body):
                raise urllib.error.HTTPError(e.url, e.code, e.msg, e.headers, io.BytesIO(text.encode("utf-8")))
            resp = self.upstream(route, "/chat/completions", body)
        with resp:
            if not req.get("stream"):
                out = from_openai(json.loads(resp.read().decode("utf-8")), asked, stops)
                row.update(input=out["usage"]["input_tokens"], output=out["usage"]["output_tokens"])
                self.send_json(200, out)
                return
            self.stream_head()
            t = StreamTranslator(asked, stops)
            for chunk in sse_events(resp):
                self.send_events(t.feed(chunk))
            self.send_events(t.end())
            row.update(input=t.usage.get("prompt_tokens"), output=t.usage.get("completion_tokens"))

    def send_events(self, events):
        if not events:
            return
        self.wfile.write(b"".join(b"event: %s\ndata: %s\n\n" % (e.encode(), json.dumps(d).encode("utf-8"))
                                  for e, d in events))
        self.wfile.flush()

    def count_tokens(self, req, route):
        body = to_openai(dict(req, stream=False), route)
        try:
            r = urllib.request.Request(route.root() + "/tokenize",
                                       data=json.dumps({"model": route.model, "messages": body["messages"]}).encode(),
                                       headers=auth_headers(route), method="POST")
            with urllib.request.urlopen(r, timeout=30) as resp:
                n = json.loads(resp.read().decode("utf-8")).get("count")
            if isinstance(n, int):
                self.send_json(200, {"input_tokens": n})
                return
        except Exception:   # noqa: BLE001 (an engine without /tokenize: estimate)
            pass
        self.send_json(200, {"input_tokens": estimate_tokens(req), "estimated": True})


def serve():
    global GATE
    GATE = Gate()
    port = int(env("GATE_PORT", "4001"))
    servers = []
    for addr in [a.strip() for a in env("GATE_BIND", "127.0.0.1").split(",") if a.strip()]:
        srv = ThreadingHTTPServer((addr, port), Handler)
        srv.daemon_threads = True
        servers.append(srv)
        print("rack gate %s on %s:%d" % (VERSION, addr, port), flush=True)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()


def main(argv):
    if argv[1:2] == ["serve"]:
        serve()
        return 0
    if argv[1:2] == ["hash"]:                  # rack gate key add: the hash of a key read from stdin
        print(sha256(sys.stdin.readline().strip()))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
