# Changelog

## 2026-11-02: an engine with its own launcher; what is loading is seen

### New
- **`ENGINE=external`**: a recipe around an engine's own start and stop commands
  (docs/05-recipe-method.md). It is recorded like any other, so the gate routes it, the
  monitor reports it, rack keep brings it back and `rack down` stops it, with no route to
  keep by hand.
- **`rack switch <recipe>`**: stop whatever serves, container or external, then serve this one
  (`rack up --replace`).
- **What is loading is seen.** `rack up` writes `starting.json` with a heartbeat. The monitor
  publishes it as a `starting` block: loading, failed with the reason, or interrupted when the
  heartbeat stops. A big model takes minutes to load, and until now that looked the same as
  nothing being there.

## 2026-10-22: rack gate replaces LiteLLM; rack keep

### Security
- **`rack gate`, the rack's own front door, replaces LiteLLM.** LiteLLM had 15
  security advisories in 2026, several critical (privilege escalation to remote
  code execution, SQL injection and authentication bypass in key handling,
  command execution through MCP endpoints); this rack's own instance was
  exposed to a critical one. The gate is one stdlib file in a read-only
  container with no capabilities and no admin endpoints, database, plugins,
  MCP or templates. README, "Why rack gate, not LiteLLM"; docs/14-gate.md.

### New
- **rack gate**: OpenAI chat, completions and embeddings passed through, and
  Anthropic's Messages API translated (text, images, tools, thinking,
  streaming, count_tokens), so Claude Code and the Anthropic SDKs work against
  the rack. Routes come from what is serving; only models that answer are
  listed; an answer too big for the engine's window is fitted; a hashed key per
  client (`rack gate key add|ls|rm`); a usage log without prompts.
- **rack keep**: cron asks the engine every 2 minutes; one that stays dead for
  15 minutes is served again (at most once per 30 minutes, three times a day).
  A cluster's engine runs inside an idle container, so before this a dead engine
  left its containers "Up" and Docker never restarted them. Needs no linger, so
  it also re-forms the cluster after a reboot.

### Fixed
- llama.cpp b11675's assets: the bump had kept b11430's file names and digests,
  so a new Mac or CUDA install asked for a file that does not exist.

### Still there
- `rack gateway` keeps a LiteLLM's routes for a site that wants LiteLLM's extras.
