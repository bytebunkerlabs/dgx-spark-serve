# rack gate: the rack's own front door

One address and a key per client for every model the rack serves, in the
OpenAI API and in Anthropic's Messages API, with nothing else to it. It
replaces LiteLLM for what a rack uses LiteLLM for: one front door, a public
name per model, routing to a few engines. (`rack gateway` still keeps a
LiteLLM's routes for a site that wants LiteLLM's extras.)

```
rack gate up                      start it (a container on the head)
rack gate status                  where it listens, what it routes, its clients
rack gate key add <client> [--models a,b] [--stdin]   a key, shown once (or read from stdin)
rack gate key ls | rm <client>
rack gate logs [-f]
rack gate down
```

## What it serves

| Path | API | Notes |
|---|---|---|
| `GET /health` | none | `{"ok": true}`, no key |
| `GET /v1/models` | OpenAI, or Anthropic's shape when the request carries `anthropic-version` or `x-api-key` | only models that answer now, and only those the key may use |
| `POST /v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | OpenAI | passed through: the model name is rewritten to the engine's, the engine's key added; streams are relayed as they come |
| `POST /v1/messages` | Anthropic Messages | translated to and from chat completions: text, images, tools (`tool_use` / `tool_result`), thinking, stop sequences, streaming events; errors in Anthropic's shape |
| `POST /v1/messages/count_tokens` | Anthropic | the engine's `/tokenize` when it has one, else an estimate (characters / 4), marked as such |

Claude Code and the Anthropic SDKs use the rack with
`ANTHROPIC_BASE_URL=http://<head>:<port>` and `ANTHROPIC_API_KEY=<a gate key>`.
They name Claude models; `aliases` in `gate/gate.json` map those names (globs) to the
rack's models, e.g. `"claude-*": "glm-5.3-flash-uncensored"`.

### Claude Code on the rack

```
rack gate key add my-laptop            # on the head; the key is shown once
export ANTHROPIC_BASE_URL=http://<head's tailnet address>:4001
export ANTHROPIC_API_KEY=<that key>
claude
```

With `"claude-*": "<a rack model>"` in `gate/gate.json`'s aliases, every model
Claude Code asks for is served by that model. Claude Code asks for answers up to
128k tokens; the gate fits the answer to the engine's window (from
`serving.json`'s context, then from the engine's own refusal, once).
Measured 2026-10-21 with GLM-5.3-Flash on two Sparks: a Read tool call and its
answer in two turns, 13.6 s, about 10.5k input tokens a turn.

## Routes

From what is serving, never a hand-kept list:

- the head's `serving.json`: the recipe's public name (`GATEWAY_NAME`, else the
  served name) goes to `http://127.0.0.1:<port>/v1` with the engine key;
- `~/.config/dgx-serve/gate/gate.json`, for engines dgx-serve does not run here:
  `{"routes": [{"name": "qwen3-4b-fast", "url": "http://10.0.0.7:8001/v1",
  "model": "qwen3-4b-fast", "key_file": "…"}], "aliases": {"old-name": "new-name"}}`.

A model is listed only while its engine's `/v1/models` names it (asked at most
every 10 s), so a stopped engine leaves the list at once.

Thinking: an Anthropic request's `thinking` block turns the model's thinking
switch (the recipe's `DIALECT_THINKING`, e.g. `chat_template_kwargs.enable_thinking`)
on or off; the reply's `reasoning_content` comes back as a `thinking` block.

## Keys

`~/.config/dgx-serve/gate/keys.json` (0600): per client a name, the key's
sha256 (never the key), the models it may use (`*` by default) and when it was
made. Sent as `Authorization: Bearer <key>` or `x-api-key: <key>`. The gate
re-reads the file when it changes, so a key added or removed works at once.
`--stdin` takes a key you already have (for example the old LiteLLM master key,
so clients move without a change), never on a command line.

## Usage

One line per request in `~/.local/state/dgx-serve/gate/usage-<day>.jsonl`:
time, client, model, API, status, input and output tokens, milliseconds. No
prompts, no answers.

## What it does not have, on purpose

No admin endpoints, no web UI, no database, no plugins, no MCP, no templates,
no code execution, no outbound calls but to the engines in its routes. The
parts behind LiteLLM's critical advisories in 2026 (admin login, MCP test
endpoints, SQL key lookups, prompt templates) do not exist here.

## How it runs

A container `rack-gate` on the head (python:3.12-slim plus one stdlib
script, `gate/rackgate.py`), `--restart unless-stopped`, host network so it
listens only on the addresses in `GATE_BIND` (default: loopback and the tailnet
address), read-only, `--cap-drop ALL`, no-new-privileges, a memory and process
cap, as your user. It mounts read-only: the serving record, `gate.json`, the
keys and the engine key; read-write: only its usage folder.

`rack.env`: `GATE_PORT` (4001 beside LiteLLM's 4000; 4000 once LiteLLM is
retired), `GATE_BIND` (comma-separated addresses).

## Moving off LiteLLM

1. `rack gate up` on 4001, beside LiteLLM's 4000.
2. `rack gate key add hermes --stdin < old-master-key` (the same key, so
   clients change only the port), or a new key per client.
3. Point one client at 4001; compare for a few days.
4. Stop LiteLLM, set `GATE_PORT=4000`, `rack gate up` again.

## Compared with LiteLLM (2026-10-22)

LiteLLM is a gateway and SDK for 100+ providers with a database, an admin UI,
teams, budgets, MCP and agent gateways. A rack uses a small part of it. The gate
builds that part, adds what a rack needs that LiteLLM does not do, and leaves out
the rest on purpose. Status: **yes** (in the gate), **added** (the gate does it,
LiteLLM does not), **next** (worth building soon), **later** (when a rack needs
it), **no** (left out by design).

| Area | LiteLLM | rack gate | Status |
|---|---|---|---|
| **APIs** | | | |
| Chat completions, completions, embeddings | yes | passed through, renamed, engine key added | yes |
| Anthropic Messages, count_tokens, models | yes | translated to the engine's chat API | yes |
| OpenAI Responses API (Codex CLI) | yes | | next |
| Rerank, images, audio (speech, transcription) | yes | passthrough once a rack serves such a model | later |
| Realtime (WebSocket, WebRTC) | yes | | later |
| Batches, files, fine-tuning, evals, assistants, vector stores, containers, memory, skills, OCR, search, videos, Gemini/Bedrock-native routes | yes | stateful or cloud products | no |
| **Providers** | | | |
| 100+ providers, each translated | yes | any OpenAI-compatible server (vLLM, llama.cpp, TensorFold, SGLang, Ollama's /v1, an OpenAI-compatible cloud) | yes, by design |
| Bedrock, Vertex, Gemini, Azure native formats | yes | | no |
| **Routing** | | | |
| Model aliases, wildcards | yes | `aliases` with globs | yes |
| Routes kept by hand in YAML | yes | from what is serving (`serving.json`) plus `gate.json` | added |
| Only models that answer are listed | no (lists every configured route) | yes, asked every 10 s | added |
| Answer fitted to the engine's window | context-window fallbacks | caps max_tokens, retries once with the engine's own numbers | added |
| Load balancing across copies of a model | yes | | next |
| Fallback to another model on error | yes | | next |
| Retries, cooldowns, per-route timeouts | yes | one fixed read timeout | later |
| drop_params (strip what an engine refuses) | yes | not needed so far | later |
| Tag routing | yes | | no |
| **Access** | | | |
| A key per client, models it may use | virtual keys (database) | sha256 in a 0600 file, re-read live | yes |
| Key expiry | yes | | next |
| Per-key request limits (RPM, parallel) | yes | | next |
| Teams, organizations, users, customers | yes | one owner's rack | no |
| JWT, OIDC, SSO, SAML | yes (some enterprise) | tailnet and keys | no |
| Budgets and cost in dollars | yes | the app's Usage screen prices tokens | no |
| **Seeing it** | | | |
| Per-request usage log | database, UI | JSONL: client, model, tokens, ms; never prompts | yes |
| Prometheus metrics | yes | | next |
| Logging callbacks (Langfuse, Datadog, OpenTelemetry...) | yes | nothing leaves the network | no |
| Alerts (Slack, webhooks) | yes | | no |
| Admin web UI | yes | the ByteBunker app, later | no (in the gate) |
| **Other** | | | |
| Response caching (Redis, in-memory, semantic) | yes | the engines' prefix caches do the real work | no |
| Guardrails, PII masking | yes (enterprise) | | no |
| Prompt management and templates | yes | | no |
| MCP gateway, A2A agent gateway | yes | the app hosts MCP; agents run on their own worker | no |
| Postgres, Redis | needed for keys and spend | no state but two files | no |
| Secret managers (Vault, AWS, Azure, GCP) | yes | 0600 files | no |
| Hot reload | config needs a restart; keys live | keys and gate.json re-read live | yes |
| Hardened container (read-only, no capabilities) | no | yes | added |
| Security advisories in 2026 | 15, several critical | | |
