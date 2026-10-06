# The recipe method

*How to go from "here is a model" to "here is a running endpoint and a number",
repeatably, on hardware nobody wrote a guide for.*

---

## The thesis

Every model is different. The **questions** are not.

A deployment recipe is not a pile of flags. It is a set of **answers to eight
fixed questions**, written down in an order where each one can veto the next.
That is why a template is possible at all: you are not templating the answers,
you are templating the interrogation.

Once you see it this way, the flags stop being folklore. `--quantization fp8`
in one recipe and `--kv-cache-dtype fp8` in another are not two spells that
happened to work. One is an answer to *"does it fit"* and the other is an answer
to *"what does this attention layout refuse to accept"*. Different questions,
different slots, different reasons to delete them later.

---

## The layering rule

Before the questions, the rule that keeps recipes small:

> **If it is true for every model, it must not be in a recipe.**

Three layers, and everything belongs to exactly one:

| Layer | Holds | Changes when |
|---|---|---|
| **Launcher** | fabric setup, mounts, image handling, node-rank plumbing, offline/telemetry defaults | your hardware changes |
| **Recipe** | the eight answers below | the model changes |
| **Invocation** | which node, which port, run label | the run changes |

You can check your own layering in one glance: **how short is your thinnest
recipe?** If a variant recipe — same model, one node over — is longer than a
few lines, plumbing has leaked upward into the model layer. In this repo
`recipes/h3-spark2-sage.env` is two lines: a source of its sibling, and one
`--host`. Everything else was already true somewhere it belonged.

---

## The eight questions

### 1. Fit — does it physically fit?

First, always, and it is arithmetic rather than opinion: bytes on disk versus
(usable memory × nodes − a reserve for KV cache and runtime).

The trap is circularity. If the model only fits *because* you quantize it, then
your fit calculation must be run against the quantized size, and the
quantization flag is now **load-bearing** — not an optimization someone can
tidy away. Mark it as such in the recipe.

The second trap is trusting the wrong instrument. On unified memory
`nvidia-smi` is blind; `free -h` is truth. Measure with the tool that can see.

**Deliverable:** a number and a verdict, recorded in the recipe.

### 2. Engine — does something exist that runs it *on this hardware*?

The question is never "does vLLM support this model." It is "does a build
supporting this model exist **for my architecture**." Those are different dates,
and the gap between them is where most days die.

Upstream can land a model on Monday, cut a release Tuesday, and merge the fix
your GPU needs on Wednesday — 27 hours after the tag, which means no published
image has it. A model's support PR and your architecture's kernel build are
independent timelines.

Three honest outcomes, in preference order: a released image that has it; a
community or nightly build for your arch; a released image plus a pinned patch
(question 7). "Latest" is not an answer — pin the digest.

### 3. Access — can you actually get the weights?

Gated repos, license acceptance, tokens mounted where the *container* reads them
rather than where you happen to be logged in. And partitions: a repo can be
500 GB while the part you need is 144.

**Record the exact download command that got the right subset.** You will not
re-derive it, and a re-download at this size is measured in hours.

### 4. Shape — the flags that make it fit and start

Sizing knobs, not speed knobs. Raising them does not make it faster; it makes it
fail later and more confusingly.

The pair that matters most trades against itself: **context length × concurrent
sequences** both consume the same KV pool. Never raise both. Pick which one the
workload actually needs and starve the other.

### 5. Dialect — the flags that make the output *correct*

This is the question people skip, because skipping it doesn't crash anything. It
returns answers that are merely *present* rather than *right*: no reasoning
content, tool calls as prose, a parser patiently waiting for a span the model
was never told to emit.

**Read the tokenizer implementation in your serving engine, not just the model
card.** Vendor documentation describes the vendor's hosted API; your engine is a
reimplementation and its defaults have disagreed. In this repo, DeepSeek's docs
say thinking is on by default; vLLM's tokenizer says `thinking = False` unless
asked. Both are accurate. Only one is running on your machine.

Also record per-request settings here even though they are not flags. They are
part of the deployment contract; if they live only in a client somewhere, the
knowledge is lost.

### 6. Environment — what the hardware demands

Collectives that assume an NVLink-class fabric and hard-fail on RoCE. Compile
modes that converge on one topology and grind for hours on another. Timeouts
sized for a machine that loads faster than yours.

The timeout class deserves its own warning, because the failure is absurd: a
model can finish initialising **twelve seconds** past a default and be killed
having just become ready. Nothing is wrong. The number was wrong.

### 7. Patch — what upstream hasn't landed yet

Sometimes the fix exists as a PR and not as a release. Vendoring it is
legitimate. Vendoring it *carelessly* is how a temporary hack becomes permanent
infrastructure.

Every patch needs three things recorded next to it: **what it fixes, the
upstream reference, and the condition under which it is deleted.** The third is
the one everyone omits and the only one that has a future.

Corollary, learned the expensive way: **never run against an engine whose patch
failed to import.** Gate the build on an import check so a broken overlay cannot
be tagged. An engine that starts with a dead backend will accept your request,
answer health checks, and destroy the run.

### 8. Proof — how you know it works

"It started" is not proof. A number is proof.

Decode rate, time to first token, KV pool size, peak memory. Taken the same way
each time, committed alongside the recipe, so the next change has something to
be compared against.

And when no published number exists for that model on that hardware — which,
off the mainstream path, is often — say so plainly. That is not a gap in your
work. That makes your number the first one.

---

## The failure ladder

Recipes are not written. They are *survived*, then written down.

The discipline that makes the survival reusable: when something fails, record
the rung — the symptom, the cause, the fix — inside the artifact, at the flag it
concerns. Not in a scratch file, not in your head, not in a chat log.

This is what separates a recipe from a command someone once ran. A year later
the flags are identical either way. Only one of them can be safely changed.

Two rungs from this repo, as the shape of it:

- `FULL_AND_PIECEWISE` cudagraph capture compiled for 4.75 hours on a cross-node
  link without converging. `PIECEWISE` is not a preference here; it is a
  measurement, and the comment says so.
- Stream-copy concatenation across engine sessions exits 0 and produces a video
  frozen at every segment boundary. Success codes lie. The fix — always
  re-encode — lives in the code that would otherwise be "optimised" back.

---

## What "done right" means

A recipe is done when someone else — including you, six months out — can read it
top to bottom and answer three questions without running anything:

1. **Why is each flag there?** (every non-obvious one carries its reason)
2. **Which flags can I delete?** (temporary things carry their deletion condition)
3. **What did it do?** (a measured number, not an adjective)

If all three are answerable, the flags are documentation. If none are, you have a
command that happened to work once, and no way to tell the difference between
tuning it and breaking it.

---

## Doing it

```
rack fit  <org/model>          # question 1 — before anything else
rack new  <name> <org/model>   # recipes/<name>/: model.env + this platform's file (--mac, ...)
                               # answer 2-7 in the file, in order
rack pull <org/model>
rack up   <name>
rack bench <name>              # question 8
```

The templates are `recipes/TEMPLATE.env` (a vLLM variant: DGX Spark, NVIDIA
Linux) and `recipes/TEMPLATE-llamacpp.env` (a llama.cpp variant: Mac, Windows).
Each is the eight questions with the evidence slots left blank; `rack new` puts
the model's own answers (question 3, and the per-request half of 5) in the
recipe's `model.env`, shared by every platform. `rack recipes check` says what a
recipe may contain (docs/12-platforms.md). Copy it; do not start from a blank file, and do not
start from a recipe for a different model — inherited flags whose reasons no
longer apply are the most expensive kind.
