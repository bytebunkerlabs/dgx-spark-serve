# 12 — Platforms: one `rack` on a DGX Spark, NVIDIA Linux, Windows and a Mac

*dgx-serve 1.0 serves models on four kinds of machine with the same commands.
This page is the reference: what each platform needs, what `rack` does there,
and every setting that changes it. The two-Spark runbooks (docs/00 to 04)
still hold for the fabric, the images and the recipe method; docs/06 and 07
describe `rack` as it was before 1.0.*

---

## The four platforms

| platform | `rack platform` says | engine | how it runs | kept alive by | monitor |
|---|---|---|---|---|---|
| DGX Spark (GB10) | `dgx` | vLLM, the rack's sm_121 image | Docker | `--restart unless-stopped`; one model across Sparks: a boot unit on the head | containers |
| NVIDIA Linux, one or more GPUs | `linux` | vLLM (`vllm/vllm-openai`), or llama.cpp CUDA for small GPUs | Docker, or native for llama.cpp | the restart policy, or a systemd user unit | containers, or bare |
| Windows 11 with WSL2 | `windows` | llama.cpp CUDA, or vLLM from a venv | native in WSL2 (Docker when it is there) | a systemd user unit | bare |
| Apple Silicon Mac | `mac` | llama.cpp with Metal | native | launchd | bare |

**Supported for 1.0** (`rack init` refuses anything else and says why; dgx-serve
never installs GPU drivers): DGX OS; Ubuntu 22.04 or 24.04 on x86_64 or arm64
with an NVIDIA driver; Ubuntu 24.04 under WSL2 on Windows 11 (build 22000 or
later); macOS 14 or later on Apple Silicon. An Intel Mac, WSL2 without a GPU and
Linux without an NVIDIA GPU are refused with the reason. `rack init --force`
goes ahead on an unsupported *version*, never on a machine that cannot serve.

```bash
rack platform            # what this machine is, its GPUs, and the memory models get
rack platform --json     # the same for the app
```

The memory a model may use is the platform's own number: unified memory on a
Spark, the GPU's memory on Linux and Windows, and on a Mac the Metal working-set
limit (about two thirds of RAM up to 36 GB, three quarters above, unless
`iogpu.wired_limit_mb` was raised).

## First run on any machine

```bash
git clone https://github.com/bytebunkerlabs/dgx-spark-serve.git ~/dgx-serve
cd ~/dgx-serve && ./rack install      # rack on your PATH
rack init                             # detect, name this machine, check what serving needs
rack recipes --mac                    # (or --dgx, --linux, --windows) what fits here
rack up qwen3-8b                      # pulls what it needs first, returns when healthy
rack chat "hello"
rack monitor up                       # telemetry for the ByteBunker app
```

On a rack of several machines, clone it at the same path on each (rack looks
for itself there over ssh; `rack nodes add --rack-dir` says otherwise).

`rack init` is safe to run again. It writes `~/.config/dgx-serve/`:

| file | what |
|---|---|
| `nodes/<name>.env` | the inventory: one file per machine (this one, its workers, other machines you serve on) |
| `engine.key`, `engine.env` | the engine's API key (0600, never printed); `rack up` hands it to the engine |
| `rack.env` | site settings (optional; the checkout's `.env` from before 1.0 still works and wins) |
| `recipes/` | your own recipes, kept across updates; a name here hides the checkout's |
| `mods/` | your own mods, for your own recipes: `MODS=(mods/<name>)` looks here when the checkout has no such mod |
| `hf-token` | a Hugging Face read token, for gated models (optional) |

It lists what serving here still needs, each with its fix, and exits non-zero
while anything is missing. `rack init --json` is the same for the app.

## The inventory: one machine or many

```bash
rack nodes                                 # the machines rack knows
rack nodes add spark-2 --fabric 192.168.100.2   # a worker: one model across the head and it
rack nodes add mini --ssh me@mini.local    # a machine that serves its own model
rack nodes test                            # ssh, platform, rack installed, fabric
rack nodes rm mini                         # forget it (touches nothing on it)
```

`add` asks the machine over ssh what it is (rack need not be installed there
yet), so `--dgx`, `--linux`, `--windows` or `--mac` is only needed when it is
unreachable (`--no-probe`). A node with a fabric address is a **worker** of this
head; without one it serves on its own (**node**) and you reach it with `--on`.
Workers share the head's platform; Macs and Windows PCs serve one model per
machine.

**The author's rack, and `.env` from before 1.0.** `rack init` imports
`HEAD_IP`, `WORKER_SSH`, `WORKER_IP`, `FABRIC_IF` and `IB_HCAS` the first time.
Until then, a DGX Spark that owns `192.168.100.1` is spark-1 with spark-2 as
its worker, as it always was; any other machine without an inventory is a
single-node site with no phantom worker. An empty `WORKER_SSH=` stays empty.

## Platform flags, `--plan` and `--on`

`rack pull`, `up`, `fit`, `recipes` and `new` take `--dgx`, `--linux`,
`--windows` or `--mac`. Without one, rack uses this machine's platform. A flag
this machine cannot honour is refused with the fix:

```
this is a DGX Spark; --mac needs a Mac: run it there, or add the Mac with rack nodes add and use --on.
```

```bash
rack up qwen3-8b --mac --on mini      # the same command, on that machine, over ssh
rack up qwen3-8b --windows --plan     # what would run, from any machine
rack up qwen3-8b --plan --json        # every step, on every node, as data
```

`--on` refuses a node whose rack is older than this one (it cannot answer
`rack version --json`): an older rack ignores flags it does not know, so a
`--plan` there would launch instead of plan.

## Recipes: a folder per model, a file per platform

```
recipes/qwen3-8b/model.env     MODEL, ROLES, DIALECT_*: what every platform shares
recipes/qwen3-8b/dgx.env       ENGINE=vllm,     SERVE_ARGS for a Spark
recipes/qwen3-8b/linux.env     ENGINE=vllm,     IMAGE, SERVE_ARGS for a 24 GB GPU
recipes/qwen3-8b/windows.env   ENGINE=llamacpp, ARTIFACT=<org>/<repo>/<file>.gguf, SERVE_ARGS
recipes/qwen3-8b/mac.env       ENGINE=llamacpp, ARTIFACT, SERVE_ARGS
```

Each platform file sources `"$RECIPE_DIR/model.env"`. A flat `recipes/<name>.env`
from before 1.0 is a vLLM container recipe and keeps working on dgx and linux.

```bash
rack recipes                       # every recipe and its platforms
rack recipes --mac [--json]        # on a Mac: only what fits this Mac
rack recipes check [<name>...]     # what may be in a recipe; errors fail
rack new tiny org/Tiny-1B --mac    # a folder with model.env and mac.env
rack new tiny --dgx                # add a platform to it
rack new phase2-gpt-oss-120b-solo --mac   # a flat recipe becomes a folder (git mv)
```

Recipes are sourced bash, so a recipe may only assign variables and source its
own `model.env` (or a flat parent). Command substitution, backticks, process
substitution, `;`, `|`, `&`, commands and any other `source` are errors, and
rack never sources a file, for a listing or a launch, before it and
everything it sources pass. The launcher owns `--host`, `--port`, the served
name and the key: a 1.0 recipe that sets them is an error (a recipe from before
1.0 is honoured, with a note).

The variables a recipe may set, besides `SERVE_ARGS`, `ENV_EXTRA` and `MODS`:

| variable | file | meaning |
|---|---|---|
| `MODEL` | model.env | the Hugging Face repo |
| `ROLES` | model.env | what the app may send it: chat tools reasoning vision code embedding rerank draft |
| `DIALECT_*` | model.env | client rules: `THINKING`, `EFFORT`, `STRIP_REASONING`, `ECHO_REASONING`, `MIN_MAX_TOKENS`, `SAMPLING` |
| `GATEWAY_NAME` | model.env | its route on a LiteLLM gateway (default: the recipe's name) |
| `ENGINE` | platform | `vllm` or `llamacpp` (dgx and linux default to vllm) |
| `IMAGE` | platform | a vLLM image (default: the rack's on a Spark, `vllm/vllm-openai:v0.26.0` on Linux) |
| `ARTIFACT`, `ARTIFACT_REVISION`, `ARTIFACT_MMPROJ` | platform | llama.cpp's GGUF file, the commit, a vision projector |
| `WEIGHTS_GB` | platform | the download size, for `rack recipes --<platform>` |
| `SERVED_NAME` | either | the model id the API serves (default: the recipe's name) |

Context, tool and reasoning parsers, vision and speculative decoding are read
from `SERVE_ARGS`; `rack recipes --json` reports them per variant.

## Weights: `rack pull`

```bash
rack pull qwen3-8b            # what this platform's variant serves from
rack pull qwen3-8b --plan     # size, what is already here, free space
rack pull org/name            # a repo: vLLM's weights (safetensors, configs, tokenizer)
```

One downloader on every platform (`py/hfget.py`, standard library only): it
lists the repo at its revision, checks the disk first, downloads in parallel
with resume, verifies every file against the hub's sha256 (or git blob id)
before it lands, and writes the hub's own cache layout under `HF_CACHE`, so
vLLM and llama.cpp find it. A llama.cpp variant fetches exactly its one GGUF
file. On a head with workers, vLLM weights are then copied to every worker over
the fabric and the shards verified on each. Gated models need a read token in
`~/.config/dgx-serve/hf-token` (or `HF_TOKEN`); it goes to the hub only, never to
the CDN it redirects to, and is never printed. `HF_ENDPOINT` points elsewhere.
`rack up` pulls first when weights are missing.

## Will it fit: `rack fit`

```bash
rack fit qwen3-8b             # at the recipe's context window
rack fit Qwen/Qwen3-8B-GGUF   # every quant, with the longest window that fits
```

The hub's numbers (download size, parameters, experts per token, KV cache per
token from `config.json`) against this machine's budget, minus what the
platform keeps for itself (9 GB on a Spark, under a GB beside a discrete GPU),
plus the engine's own memory. On a Spark it also answers for every Spark in the
rack. Another platform's answer comes from a machine of it: `--on`.

## Serving: `rack up` and `rack down`

`rack up` plans every step as data (`--plan` shows it) and then runs it. It
always detaches: it streams the engine's log until `/health` answers and
returns; Ctrl-C detaches, the engine keeps starting.

Before it starts anything it refuses to land on another engine: rack's own
(`--replace` swaps it), a container named in `FOREIGN_ENGINES`, or anything
listening on the port. It pulls missing weights, builds or pulls the image, and
on a Spark drops the page cache and starts `scripts/memwatch.sh` beside the
engine.

- **vLLM in Docker** (dgx, linux): `docker run -d --restart unless-stopped`,
  the key through `--env-file` (`VLLM_API_KEY`), the weights read-only from the
  cache, `HF_HUB_OFFLINE=1` and no usage stats, journald logs, and a memory cap
  from the machine's RAM: total minus 9 GiB on a Spark (112 GiB on a GB10), minus
  8 on Linux, minus 4 in WSL2 (`MEM_CAP_GB` sets it, 0 turns it off).
- **One model across machines** (Sparks; pipeline parallel across Linux boxes):
  nodes = tensor parallel x pipeline parallel on Sparks. One keep-alive
  container per node with its own `VLLM_HOST_IP`, NCCL interface and RoCE HCAs;
  the same image on every node (`rack build` ships it); the shards on every node;
  workers launched before the head; all detached. A systemd user unit on the
  head re-forms it after a reboot (`rack up --boot`, which waits for the
  workers); it needs linger: `sudo loginctl enable-linger $USER`.
- **llama.cpp, natively** (mac, windows, linux): the pinned build b11430 from
  ggml-org's releases, verified by sha256 and unpacked under
  `~/.local/state/dgx-serve/engines/`. CUDA builds come with their cudart bundle
  (WSL2 has the driver but not the runtime libraries) and need a driver with
  CUDA 12.8 or newer. Under launchd on a Mac, a systemd user unit elsewhere
  (with `MemoryMax`), with `--api-key-file`, `--metrics` and the log in
  `~/.local/state/dgx-serve/logs/engine.log`.
- **vLLM from a venv** (Windows without Docker): vLLM 0.26.0 in
  `~/.local/state/dgx-serve/engines/vllm/`, under a systemd user unit.

`rack up` records what it started in `~/.local/state/dgx-serve/serving.json`
(recipe, engine, runtime, nodes, port, roles, dialect, context); the monitor
publishes it to the app. `rack down` stops exactly that, on every node, removes
the units and the boot unit, and removes the record. `rack status`,
`rack logs [-f] [<node>]`, `rack chat` and `rack bench` all follow it.

### The engine key and where the engine listens

`rack init` (or the first `rack up`) makes `~/.config/dgx-serve/engine.key`.
The engine then refuses requests without it (401), and listens on `0.0.0.0`
so the app can reach it from another machine. `rack status`, `rack chat` and
`rack bench` send it. To serve without a key, set `ENGINE_KEY=off`: the engine
then listens on `127.0.0.1` only, unless `ENGINE_BIND` says otherwise.

**Before 1.0 reaches a rack whose gateway talks to the engine without a key**
(the author's LiteLLM): either give LiteLLM the key (the gateway section below)
or set `ENGINE_KEY=off` and `ENGINE_BIND=0.0.0.0` in `rack.env` first.

## A LiteLLM gateway, if the site has one

No gateway by default. With `GATEWAY_CONFIG` pointing at a LiteLLM config,
`rack up` adds the serving recipe's route once it is healthy and `rack down`
removes it:

```yaml
model_list:
  # >>> dgx-serve managed: rack up and rack down keep the routes in here
  - model_name: qwen3-8b
    litellm_params:
      model: openai/qwen3-8b
      api_base: http://192.168.100.1:8888/v1
      api_key: os.environ/DGX_SERVE_ENGINE_KEY
  # <<< dgx-serve managed
```

rack edits only between those lines, by scanning them (no YAML library), so a
hand-kept config keeps its comments; the previous file stays as `.bak`. A
name already routed by hand is refused until `rack gateway adopt <name>` moves
it in. LiteLLM needs `DGX_SERVE_ENGINE_KEY` (the engine key) in its environment.

```bash
rack gateway              # the config, what rack keeps, what is by hand
rack gateway sync         # the route for what is serving now
rack gateway remove <name>
rack gateway adopt <name>
```

| setting | meaning |
|---|---|
| `GATEWAY_CONFIG` | the LiteLLM config on this machine (turns the gateway on) |
| `GATEWAY_CONTAINER` | its container: restarted after a change |
| `GATEWAY_URL`, `GATEWAY_KEY_FILE` | where to check the route appeared (`http://127.0.0.1:4000`), and LiteLLM's master key (raw, or `LITELLM_MASTER_KEY=` line) |
| `GATEWAY_ENGINE_HOST` | the address LiteLLM reaches the engine at (default: the head's fabric address, else its LAN address) |

## Per platform

### DGX Spark

DGX OS with Docker and the NVIDIA container toolkit, as shipped. `rack build`
makes the rack's image (the community profile, with sm_121 kernels) and ships
it to every worker. Two or more Sparks: cable the fabric (docs/00), `rack init`
on the head, `rack nodes add <worker> --fabric <ip>` for each, `rack preflight`,
then `rack up` a recipe with `--tensor-parallel-size 2` (or more). The head's
own fabric address is recorded from the route to the first worker (or
`rack init --fabric <ip>`).

**Dropping the page cache without a password.** On unified memory, cached file
pages and the model share one pool; `rack up` drops the cache before a big
load and warns when it cannot. Allow exactly that, once per Spark:

```bash
sudo tee /usr/local/sbin/drop-caches >/dev/null <<'EOF2'
#!/bin/sh
sync && echo 3 > /proc/sys/vm/drop_caches
EOF2
sudo chmod 755 /usr/local/sbin/drop-caches
echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/drop-caches" | sudo tee /etc/sudoers.d/dgx-serve-drop-caches
sudo chmod 440 /etc/sudoers.d/dgx-serve-drop-caches
```

`scripts/memwatch.sh` watches memory pressure (PSI) beside every Spark engine
and logs to `~/.local/state/dgx-serve/logs/`. It only observes until
`MEMWATCH_KILL=1`; `scripts/swap-watchdog.sh` swaps a running one for an
observe-mode one without touching the engine.

### NVIDIA Linux

Ubuntu 22.04 or 24.04, the NVIDIA driver, Docker Engine with your user in the
`docker` group, and the NVIDIA container toolkit (`nvidia-container-toolkit`,
then `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker).
`rack init` checks each and prints the missing step. vLLM runs from
`vllm/vllm-openai`; tensor parallelism uses the GPUs inside the box
(`TOPOLOGY=solo` keeps every recipe on one machine). Turing GPUs (RTX 20xx) are
better served by a llama.cpp variant (`ENGINE=llamacpp`).

### Windows (WSL2)

Windows 11, the NVIDIA driver installed **on Windows** (not inside WSL2), and
Ubuntu 24.04 in WSL2 with systemd on (`[boot]` / `systemd=true` in
`/etc/wsl.conf`, then `wsl --shutdown` in Windows). Clone and run `rack` inside
WSL2, and keep weights in the WSL2 filesystem (`~/dgx/hf`), not under `/mnt/c`.
Engines run as systemd user units; `sudo loginctl enable-linger $USER` keeps
them up after you close the terminal. Windows does not start WSL2 at boot by
itself: until the Windows installer sets up a startup task for it, open your
WSL2 distribution once after a reboot, and systemd starts the engine.

### Mac

macOS 14 or later on Apple Silicon. Nothing to install beyond `git` and
`python3` (the Command Line Tools have both): `rack up` fetches the pinned
llama.cpp build itself. Engines and the monitor run as launchd agents of your
user (`~/Library/LaunchAgents/ai.bytebunker.dgx-serve.*.plist`), so they start
at login. The first time an engine listens on `0.0.0.0`, macOS may ask whether
to accept incoming connections.

## Site settings

`~/.config/dgx-serve/rack.env`, or `.env` in the checkout (which wins). All
optional.

| setting | default | meaning |
|---|---|---|
| `HF_CACHE` | `~/dgx/hf` | the Hugging Face cache, same path on every node |
| `API_PORT` | 8888 | the engine's port |
| `ENGINE_KEY` | on | `off`: no key, and `127.0.0.1` only |
| `ENGINE_BIND` | 0.0.0.0 with a key | where the engine listens |
| `IMAGE` | per platform | the vLLM image when the recipe names none |
| `TOPOLOGY` | auto | `solo`: every recipe on one machine; `cluster`: at least two |
| `MEM_CAP_GB` | from RAM | the engine container's memory cap; 0 turns it off |
| `FOREIGN_ENGINES` | | other stacks' engine containers: `rack up` refuses while they run, `rack down` stops them |
| `MEMWATCH_KILL` | 0 | 1: memwatch stops the engine on a reclaim stall |
| `MASTER_PORT` | 29501 | the rendezvous port across machines |
| `HEAD_IP`, `WORKER_SSH`, `WORKER_IP`, `FABRIC_IF`, `IB_HCAS` | | before 1.0; `rack init` moves them into the inventory |
| `GATEWAY_*` | | the LiteLLM gateway, above |
| `MONITOR_*` | | the monitor (docs/11-monitor.md) |

## When something is off

| symptom | cause | fix |
|---|---|---|
| `this is a DGX Spark; --mac needs a Mac` | a platform flag this machine is not | run it on that machine, or `--on <node>` |
| `<recipe> has no <platform> variant (it has: ...)` | the recipe does not serve there | `rack new <recipe> --<platform>` |
| `... is already serving (...)` | another engine holds the machine or the port | `rack down`, or `rack up --replace` for rack's own |
| `still has FILL_ME` | a scaffold not yet answered | answer the questions in the file |
| `fails rack recipes check` | the recipe does more than assign variables | `rack recipes check <name>` says the line |
| `... is gated or private` | the model needs accepted terms and a token | `~/.config/dgx-serve/hf-token` |
| `needs a driver with CUDA 12.8 or newer` | an old NVIDIA driver (on Windows for WSL2) | update the driver |
| 401 from the engine | the engine key | send it (`rack chat` does); `ENGINE_KEY=off` for none |
| `the rack on <node> is older than this one` | `--on` to a node with an older dgx-serve | update dgx-serve there |
| `not healthy after ... s` | still loading, or failed | `rack logs -f` |
