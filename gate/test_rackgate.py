"""rack gate against a fake engine: keys, live routes, OpenAI passthrough,
and Anthropic Messages translated both ways, streamed and not.

    python3 -m unittest gate/test_rackgate.py -v
"""
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rackgate  # noqa: E402

ENGINE_KEY = "engine-secret"


class Engine(BaseHTTPRequestHandler):
    """An OpenAI-compatible engine serving 'glm-served': its reply depends on the last user message."""
    seen = []
    up = True

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        return self.headers.get("Authorization") == "Bearer " + ENGINE_KEY

    def do_GET(self):
        if self.path == "/v1/models" and Engine.up and self.authed():
            self.reply(200, {"data": [{"id": "glm-served"}]})
        else:
            self.reply(503 if not Engine.up else 401, {"error": "no"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Engine.seen.append((self.path, body, self.headers.get("Authorization")))
        if not self.authed():
            self.reply(401, {"error": "bad key"})
            return
        if self.path == "/tokenize":
            self.reply(200, {"count": 42})
            return
        last = next((m for m in reversed(body.get("messages") or []) if m["role"] in ("user", "tool")), {})
        text = last.get("content") if isinstance(last.get("content"), str) else json.dumps(last.get("content"))
        if "fail" in text:
            self.reply(400, {"error": {"message": "the engine refuses"}})
            return
        if "tool" in text and last["role"] == "user":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'}}]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": "hello from glm"}
            if "think" in text:
                msg["reasoning_content"] = "let me think"
            finish = "stop"
        usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
        if not body.get("stream"):
            self.reply(200, {"id": "x", "object": "chat.completion", "choices": [
                {"index": 0, "message": msg, "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = []
        if msg.get("reasoning_content"):
            chunks += [{"reasoning_content": "let me "}, {"reasoning_content": "think"}]
        if msg.get("content"):
            chunks += [{"content": "hello "}, {"content": "from glm"}]
        for tc in msg.get("tool_calls") or []:
            chunks += [{"tool_calls": [{"index": 0, "id": tc["id"], "function": {"name": tc["function"]["name"], "arguments": ""}}]},
                       {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]},
                       {"tool_calls": [{"index": 0, "function": {"arguments": '"Oslo"}'}}]}]
        for d in chunks:
            self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": d, "finish_reason": None}]}).encode() + b"\n\n")
        self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}).encode() + b"\n\n")
        if (body.get("stream_options") or {}).get("include_usage"):
            self.wfile.write(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


def start(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = start(Engine)
        d = cls.d = tempfile.mkdtemp()
        port = cls.engine.server_address[1]
        with open(os.path.join(d, "serving.json"), "w") as f:
            json.dump({"served_name": "glm-served", "gateway_name": "glm-5.3", "port": port, "key_required": True,
                       "dialect": {"thinking": "chat_template_kwargs.enable_thinking"}}, f)
        with open(os.path.join(d, "engine.key"), "w") as f:
            f.write(ENGINE_KEY + "\n")
        with open(os.path.join(d, "gate.json"), "w") as f:
            json.dump({"routes": [{"name": "elsewhere", "url": "http://127.0.0.1:9/v1"}],
                       "aliases": {"claude-*": "glm-5.3", "old-glm": "glm-5.3"}}, f)
        cls.write_keys([("app", "k-app", ["*"]), ("narrow", "k-narrow", ["nothing-*"])])
        os.environ.update(GATE_SERVING=os.path.join(d, "serving.json"), GATE_CONFIG=os.path.join(d, "gate.json"),
                          GATE_KEYS=os.path.join(d, "keys.json"), GATE_ENGINE_KEY=os.path.join(d, "engine.key"),
                          GATE_STATE=os.path.join(d, "usage"))
        rackgate.GATE = rackgate.Gate()
        cls.gate = start(rackgate.Handler)

    @classmethod
    def write_keys(cls, rows):
        with open(os.path.join(cls.d, "keys.json"), "w") as f:
            json.dump({"clients": [{"name": n, "sha256": hashlib.sha256(k.encode()).hexdigest(), "models": m}
                                   for n, k, m in rows]}, f)

    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_address[1], timeout=20)
        h = dict({"Content-Type": "application/json"}, **(headers or {}))
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (data if raw else json.loads(data or b"null")), dict(r.getheaders())

    def events(self, raw):
        out = []
        for block in raw.decode().strip().split("\n\n"):
            lines = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
            out.append((lines.get("event"), json.loads(lines["data"])))
        return out

    APP = {"Authorization": "Bearer k-app"}
    ANTH = {"x-api-key": "k-app", "anthropic-version": "2023-06-01"}

    # ---- keys and routes
    def test_health_needs_no_key_and_the_rest_does(self):
        self.assertEqual(self.req("GET", "/health")[0], 200)
        st, d, _ = self.req("GET", "/v1/models")
        self.assertEqual(st, 401)
        st, d, _ = self.req("POST", "/v1/messages", {"model": "glm-5.3"}, {"x-api-key": "wrong"})
        self.assertEqual((st, d["type"], d["error"]["type"]), (401, "error", "authentication_error"))

    def test_only_live_models_are_listed_and_only_those_a_key_may_use(self):
        st, d, _ = self.req("GET", "/v1/models", headers=self.APP)
        self.assertEqual([m["id"] for m in d["data"]], ["glm-5.3"])          # "elsewhere" does not answer
        st, d, _ = self.req("GET", "/v1/models", headers=self.ANTH)
        self.assertEqual((d["data"][0]["type"], d["data"][0]["id"], d["has_more"]), ("model", "glm-5.3", False))
        st, d, _ = self.req("GET", "/v1/models", headers={"Authorization": "Bearer k-narrow"})
        self.assertEqual(d["data"], [])
        st, _, _ = self.req("POST", "/v1/chat/completions", {"model": "glm-5.3", "messages": []},
                            {"Authorization": "Bearer k-narrow"})
        self.assertEqual(st, 404)

    def test_a_key_added_or_removed_works_at_once(self):
        self.write_keys([("app", "k-app", ["*"]), ("narrow", "k-narrow", ["nothing-*"]), ("new", "k-new", ["*"])])
        time.sleep(0.01)
        self.assertEqual(self.req("GET", "/v1/models", headers={"Authorization": "Bearer k-new"})[0], 200)
        self.write_keys([("app", "k-app", ["*"]), ("narrow", "k-narrow", ["nothing-*"])])
        self.assertEqual(self.req("GET", "/v1/models", headers={"Authorization": "Bearer k-new"})[0], 401)

    # ---- OpenAI passthrough
    def test_openai_is_passed_through_renamed_with_the_engines_key(self):
        st, d, _ = self.req("POST", "/v1/chat/completions",
                            {"model": "old-glm", "messages": [{"role": "user", "content": "hi"}]}, self.APP)
        self.assertEqual((st, d["choices"][0]["message"]["content"]), (200, "hello from glm"))
        path, body, auth = Engine.seen[-1]
        self.assertEqual((path, body["model"], auth), ("/v1/chat/completions", "glm-served", "Bearer " + ENGINE_KEY))

    def test_an_openai_stream_is_relayed_as_it_comes(self):
        st, raw, h = self.req("POST", "/v1/chat/completions", {"model": "glm-5.3", "stream": True,
                                                               "messages": [{"role": "user", "content": "hi"}]},
                              self.APP, raw=True)
        self.assertEqual(h["Content-Type"], "text/event-stream")
        self.assertIn(b'"content": "hello "', raw)
        self.assertTrue(raw.rstrip().endswith(b"data: [DONE]"))

    # ---- Anthropic Messages
    def test_a_message_and_its_reply(self):
        st, d, _ = self.req("POST", "/v1/messages", {
            "model": "claude-sonnet-4-5", "max_tokens": 300, "system": [{"type": "text", "text": "be brief"}],
            "messages": [{"role": "user", "content": "please think"}], "stop_sequences": ["END"],
            "thinking": {"type": "enabled", "budget_tokens": 1024}}, self.ANTH)
        self.assertEqual(st, 200, d)
        self.assertEqual((d["type"], d["role"], d["model"], d["stop_reason"]), ("message", "assistant", "claude-sonnet-4-5", "end_turn"))
        self.assertEqual([b["type"] for b in d["content"]], ["thinking", "text"])
        self.assertEqual(d["content"][1]["text"], "hello from glm")
        self.assertEqual(d["usage"], {"input_tokens": 11, "output_tokens": 5})
        _, sent, _ = Engine.seen[-1]
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual((sent["model"], sent["max_tokens"], sent["stop"]), ("glm-served", 300, ["END"]))
        self.assertIs(sent["chat_template_kwargs"]["enable_thinking"], True)        # the recipe's switch

    def test_tools_round_trip(self):
        tools = [{"name": "get_weather", "description": "weather", "input_schema": {"type": "object",
                  "properties": {"city": {"type": "string"}}}}]
        st, d, _ = self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 100, "tools": tools,
                            "tool_choice": {"type": "any"}, "messages": [{"role": "user", "content": "use a tool"}]}, self.ANTH)
        self.assertEqual(d["stop_reason"], "tool_use")
        use = d["content"][0]
        self.assertEqual((use["type"], use["name"], use["input"]), ("tool_use", "get_weather", {"city": "Oslo"}))
        _, sent, _ = Engine.seen[-1]
        self.assertEqual((sent["tools"][0]["function"]["name"], sent["tool_choice"]), ("get_weather", "required"))
        # the result goes back as a tool message, right after the assistant's call
        st, d, _ = self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 100, "tools": tools, "messages": [
            {"role": "user", "content": "use a tool"},
            {"role": "assistant", "content": [{"type": "text", "text": "checking"}, use]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                          "content": [{"type": "text", "text": "12 C"}]},
                                         {"type": "text", "text": "and now?"}]}]}, self.ANTH)
        self.assertEqual(st, 200, d)
        msgs = Engine.seen[-1][1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "user"])
        self.assertEqual(msgs[1]["tool_calls"][0]["function"], {"name": "get_weather", "arguments": '{"city": "Oslo"}'})
        self.assertEqual((msgs[2]["tool_call_id"], msgs[2]["content"]), (use["id"], "12 C"))

    def test_images_become_image_parts(self):
        self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 10, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            {"type": "text", "text": "what is this"}]}]}, self.ANTH)
        parts = Engine.seen[-1][1]["messages"][-1]["content"]
        self.assertEqual(parts[0], {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})

    def test_a_streamed_message_with_thinking_text_and_events_in_order(self):
        st, raw, h = self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 50, "stream": True,
                                                      "messages": [{"role": "user", "content": "think please"}]},
                              self.ANTH, raw=True)
        ev = self.events(raw)
        names = [e for e, _ in ev]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        starts = [d["content_block"]["type"] for e, d in ev if e == "content_block_start"]
        self.assertEqual(starts, ["thinking", "text"])
        text = "".join(d["delta"]["text"] for e, d in ev if e == "content_block_delta" and d["delta"]["type"] == "text_delta")
        think = "".join(d["delta"]["thinking"] for e, d in ev if e == "content_block_delta"
                        and d["delta"]["type"] == "thinking_delta")
        self.assertEqual((text, think), ("hello from glm", "let me think"))
        self.assertEqual(names.count("content_block_start"), names.count("content_block_stop"))
        md = [d for e, d in ev if e == "message_delta"][0]
        self.assertEqual((md["delta"]["stop_reason"], md["usage"]["output_tokens"]), ("end_turn", 5))
        self.assertTrue(Engine.seen[-1][1]["stream_options"]["include_usage"])

    def test_a_streamed_tool_call(self):
        st, raw, _ = self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 50, "stream": True,
                              "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
                              "messages": [{"role": "user", "content": "use a tool"}]}, self.ANTH, raw=True)
        ev = self.events(raw)
        start = [d for e, d in ev if e == "content_block_start"][0]["content_block"]
        self.assertEqual((start["type"], start["id"], start["name"]), ("tool_use", "call_1", "get_weather"))
        js = "".join(d["delta"]["partial_json"] for e, d in ev if e == "content_block_delta")
        self.assertEqual(json.loads(js), {"city": "Oslo"})
        self.assertEqual([d for e, d in ev if e == "message_delta"][0]["delta"]["stop_reason"], "tool_use")

    def test_engine_errors_come_back_in_anthropics_shape(self):
        st, d, _ = self.req("POST", "/v1/messages", {"model": "glm-5.3", "max_tokens": 10,
                                                     "messages": [{"role": "user", "content": "fail now"}]}, self.ANTH)
        self.assertEqual((st, d["type"], d["error"]["type"]), (400, "error", "invalid_request_error"))
        self.assertIn("the engine refuses", d["error"]["message"])
        st, d, _ = self.req("POST", "/v1/messages", {"model": "gpt-9", "max_tokens": 10, "messages": []}, self.ANTH)
        self.assertEqual((st, d["error"]["type"]), (404, "not_found_error"))

    def test_count_tokens_asks_the_engine(self):
        st, d, _ = self.req("POST", "/v1/messages/count_tokens", {"model": "glm-5.3",
                            "messages": [{"role": "user", "content": "hi"}]}, self.ANTH)
        self.assertEqual(d, {"input_tokens": 42})
        self.assertEqual(Engine.seen[-1][0], "/tokenize")

    def test_usage_is_logged_without_prompts(self):
        self.req("POST", "/v1/messages", {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "secret words"}]}, self.ANTH)
        folder = os.path.join(self.d, "usage")
        rows = [json.loads(l) for f in os.listdir(folder) for l in open(os.path.join(folder, f))]
        last = rows[-1]
        self.assertEqual((last["client"], last["model"], last["asked"], last["api"], last["status"]),
                         ("app", "glm-5.3", "claude-x", "anthropic", 200))
        self.assertEqual((last["input"], last["output"]), (11, 5))
        self.assertNotIn("secret", json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
