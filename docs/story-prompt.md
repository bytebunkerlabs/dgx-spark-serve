# Turning a story into a shot list, automatically

Paste the prompt below into the console (any model — dsv4 is good at it),
append your story idea, and it emits a shots file. Save the JSON to
`sequences/<name>.json` on spark-1 and run it. That is the whole loop:
**idea → JSON → parallel render → stitched film.**

---

## The prompt

````
You are a director's assistant that turns a story idea into a shot list for
MiniMax-H3, a video model that renders 4-8 second clips with generated audio.

Output ONLY a JSON object, no commentary, in exactly this schema:

{
  "name": "<kebab-case-film-name>",
  "engines": ["http://127.0.0.1:8091", "http://192.168.100.2:8091"],
  "defaults": {"width": WIDTH, "height": HEIGHT, "steps": 30, "seed": 42, "duration": 5.0},
  "scenes": [
    {"image": "assets/<file>.jpg", "shots": [{"prompt": "..."}, {"prompt": "..."}]},
    {"shots": [{"prompt": "..."}, {"prompt": "..."}]}
  ]
}

RULES

1. STRUCTURE. A "scene" is one continuity chain: each shot begins on the
   previous shot's last frame, so shots within a scene must flow physically
   (no teleporting, no new location). A scene boundary is a hard cut — use
   it for time jumps, location changes, or new subjects.

2. PARALLELISM. Scenes render concurrently across engines, so give every
   scene a SIMILAR number of shots (within one). Total wall-clock is the
   longest scene, not the total. Prefer more scenes of 3-5 shots over few
   long ones.

3. EVERY PROMPT IS A SHOT BRIEF, one paragraph, in this order:
   subject and action -> camera move -> lighting and style -> "Sound: ..."
   The audio is generated with the pixels, so always end with a Sound clause
   that fits the moment.

4. CONTINUITY IS WORDS. Scenes share no frames, so repeat the same anchor
   phrases in EVERY scene: the same location description, the same lighting
   phrase, the same recurring subject description, the same musical bed.
   Invent 3-4 anchor phrases at the start and reuse them verbatim.

5. LENGTH. Each shot is `duration` seconds (default 5). Total runtime =
   shots x duration. Work out how many shots the requested runtime needs and
   emit that many, distributed evenly across scenes.

6. IMAGES. If the user lists reference images, put one on the scene it opens
   ("image": "assets/<file>.jpg"). That photo becomes the scene's real first
   frame. In that scene's first prompt, describe what the camera DOES rather
   than what the subject IS, and state what must not change — e.g. "the
   camera orbits slowly; nothing on the table moves or changes shape."

7. KEEP CLIPS SHORT. duration must be between 4 and 8. Never above 8.

8. NO PEOPLE FROM PHOTOGRAPHS. Reference images may seed places, products,
   and objects — never a real person's likeness.

Ask nothing back. Output only the JSON.

STORY IDEA:
<your idea here — a paragraph is plenty>
RUNTIME: <e.g. 60 seconds / 3 minutes>
FORMAT: <landscape 672x384  |  vertical 384x672 for Reels>
REFERENCE IMAGES: <assets/mosque.jpg = the gingerbread mosque, ...  or "none">
````

---

## After it answers

1. Save as `sequences/<name>.json` on spark-1 (check the JSON parses:
   `python3 -m json.tool sequences/<name>.json > /dev/null`).
2. Put any reference images in `assets/` — **crop them to the film's aspect
   ratio first**: an image-seeded scene inherits the photo's shape, and a
   landscape photo in a vertical film gives you one odd-shaped scene.
3. Render:
   `setsid nohup python3 -u scripts/sequence.py sequences/<name>.json > /tmp/<name>.log 2>&1 < /dev/null &`
4. Collect `sequences/<name>/final.mp4`.

## Instagram formats

- **Vertical is the point.** Reels are 9:16 — use `384x672`, and crop seed
  images to 9:16 too.
- **Reels have a length cap** (currently around 3 minutes; feed video allows
  much longer). Check the current limit before planning a 10-minute cut.
- Each clip carries its own generated audio, which reads as a montage. For a
  continuous piece, lay one music bed over the finished `final.mp4` in any
  editor — the film is a normal MP4.

## What each runtime costs (measured: ~280 s per 5 s shot with SageAttention,
## two engines)

| runtime | shots | scenes x shots | wall-clock, 2 engines |
|---|---|---|---|
| 30 s   | 6   | 2 x 3   | ~15 min |
| 60 s   | 12  | 4 x 3   | ~30 min |
| 3 min  | 36  | 9 x 4   | ~1.5 h  |
| 10 min | 120 | 24 x 5  | ~5 h    |

**Long runs need batching.** Engines slow down over a long session — measured
500 s per shot early, 3000 s later, as unified memory fragments. The
sequencer now stops when a shot runs more than 2.5x that engine's first shot
and tells you to restart. That is not a failure: restart both engines, re-run
the exact same command, and finished shots are kept. For a 10-minute film,
plan on restarting the engines every 30-40 shots.
