# Making films on the rack

H3 renders 4–15 second clips. A film is clips edited together, and the
sequencer does the editing for you. Two rules carry everything:

**Shots chain.** Within a scene, every shot starts on the exact last frame
of the shot before it — that's the continuity. A chain is strictly
sequential: it cannot be split across engines.

**Scenes parallelize.** A scene boundary is a deliberate hard cut — no frame
crosses it. Independent scenes render concurrently, one engine each. Write
more scenes, use more Sparks.

## 0. Engines up

    spark-1:  rack up h3            # binds 127.0.0.1:8091
    spark-2:  rack up h3-spark2     # binds 192.168.100.2:8091 (fabric-only)

Check: `curl -s http://127.0.0.1:8091/health` on spark-1, same for
`http://192.168.100.2:8091/health`. The console's Video studio dot works too.

## 1. Write the shots file

`sequences/<film>.json` on spark-1, in the repo root:

    {
      "name": "my-film",
      "engines": ["http://127.0.0.1:8091", "http://192.168.100.2:8091"],
      "defaults": {"width": 672, "height": 384, "steps": 30,
                   "seed": 42, "duration": 5.0},
      "scenes": [
        {"shots": [
          {"prompt": "..."},
          {"prompt": "..."}
        ]},
        {"shots": [
          {"use": "/tmp/some-existing-clip.mp4"},
          {"prompt": "..."}
        ]}
      ]
    }

- First shot of a scene renders text-to-video (it sets the scene's look and
  resolution); later shots continue from the previous shot's last frame.
- `"use"` drops in an already-rendered clip instead of rendering — good for
  reusing an opener you like.
- Any default can be overridden per shot (`"duration": 4.0` etc.).

## 2. Write prompts like shot briefs

Subject, motion, camera, light, **and what it should sound like** — the
audio is generated with the pixels, and it follows direction ("heard
muffled, as if from the next aisle" works).

- **Repeat the world in every scene's prompts.** Scenes share no frames, so
  the words are the only continuity: same location phrase, same lighting
  phrase, same soundtrack phrase.
- **Keep clips ≤ 8 seconds.** Attention memory grows with the square of the
  frame count; a 15 s request can OOM a single Spark (measured — it also
  used to crash the queue, see vllm-omni#5793).
- Expect the world to drift *within* a shot (the pilot's basement slowly
  became a parking garage). Cuts hide drift; slow continuous takes expose it.

## 3. Budget

- Warm engine, 5 s at 30 steps: **~10 min per shot.**
- Each engine's first render after a launch may pay a one-time
  torch.compile warmup: **+25–40 min**, once.
- Wall-clock ≈ the slowest scene chain, not the sum — balance scene lengths.
- Duration is the expensive knob (quadratic-ish); steps are nearly linear.
- 5-minute film ≈ 10 scenes × 5 shots ≈ overnight on two Sparks.

## 4. Run it

From spark-1, repo root:

    setsid nohup python3 -u scripts/sequence.py sequences/my-film.json \
      > /tmp/my-film.log 2>&1 < /dev/null &

Watch: `tail -f /tmp/my-film.log` — and every render appears as a job card
in the console's Video studio while it runs.

## 5. When things go wrong

- **Transient poll timeouts** are retried automatically (the engine API gets
  slow during warmup) — log lines say so; no action needed.
- **A render actually fails**: the sequencer stops and says which engine.
  Restart that engine (Ctrl-C its rack terminal, `rack up` again — until
  vllm-omni#5793 lands, a failed render leaves that engine's queue dead),
  then re-run the same sequencer command. **Finished shots are never
  re-rendered** — it resumes where it stopped.
- The stitch always re-encodes; stream-copy concat produces files that
  freeze at cut points while claiming success (measured).

## 6. Collect

    sequences/<film>/final.mp4          # story order, re-encoded, with sound

`scp spark-1:dgx/dgx-spark-serve/sequences/<film>/final.mp4 ~/Desktop/`

Undownloaded engine-side clips die if an engine restarts — the sequencer's
copies in `sequences/<film>/` are yours and safe, but treat the engines'
own job stores as scratch.
