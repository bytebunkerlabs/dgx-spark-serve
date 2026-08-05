#!/usr/bin/env python3
"""sequence.py — chain MiniMax-H3 clips into one film. Stdlib only.

    python3 scripts/sequence.py sequences/robot-dj-encore.json

The shots file:

    {
      "name": "robot-dj-encore",
      "engine": "http://127.0.0.1:8091",
      "defaults": {"width": 672, "height": 384, "steps": 30,
                   "seed": 42, "duration": 5.0},
      "shots": [
        {"use": "/tmp/robot-dj.mp4"},
        {"prompt": "..."},
        {"prompt": "...", "duration": 4.0}
      ]
    }

A shot with "use" is an existing clip (no render) — the chain's opener.
A shot with "prompt" renders via the engine: the FIRST shot in the file
with no predecessor goes text-to-video; every later shot goes
first-frame-to-video, conditioned on the LAST FRAME of the previous
shot's clip. That is the whole continuity trick.

Everything lands in sequences/<name>/: shotNN.mp4, shotNN-last.jpg, and
final.mp4. Re-running skips shots whose mp4 already exists — a 10-hour
sequence must survive interruptions (and, until vllm-omni#5793 lands, a
failed render needs an engine restart before the next attempt).

ffmpeg runs inside the serving image (the host has none); the workdir is
bind-mounted. Frame handoff notes: fl2va ignores width/height and follows
the reference frame at a 768px short edge, so the chain's resolution is
set by shot 1 and preserved thereafter.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

FFMPEG_IMAGE = "vllm/vllm-omni:v0.26.0"
POLL_S = 15
SHOT_TIMEOUT_S = 3600


def die(msg):
    print(f"sequence: {msg}", file=sys.stderr)
    sys.exit(1)


def ffmpeg(workdir, *args):
    cmd = ["docker", "run", "--rm", "-v", f"{workdir}:/work", "-w", "/work",
           "--entrypoint", "ffmpeg", FFMPEG_IMAGE, "-v", "error", "-y", *args]
    subprocess.run(cmd, check=True)


def multipart(fields, file_field=None):
    """Encode multipart/form-data by hand — no requests on the hosts."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{k}"\r\n\r\n{v}\r\n').encode()
    if file_field:
        name, path, ctype = file_field
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"; filename="{os.path.basename(path)}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += open(path, "rb").read()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def api(engine, path, data=None, ctype=None, raw=False):
    req = urllib.request.Request(
        engine + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": ctype} if ctype else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return body if raw else json.loads(body)


def render(engine, params, ref_frame, out_path):
    fields = {
        "prompt": params["prompt"],
        "fps": "24",
        "num_inference_steps": str(params["steps"]),
        "flow_shift": "12",
        "extra_params": json.dumps({
            "task": "fl2va" if ref_frame else "t2va",
            "duration": float(params["duration"]),
            "audio_flow_shift": 3.0,
        }),
    }
    if params.get("seed") is not None:
        fields["seed"] = str(params["seed"])
    if not ref_frame:  # t2va picks the size; fl2va follows the frame
        fields["width"] = str(params["width"])
        fields["height"] = str(params["height"])
    body, ctype = multipart(
        fields, ("input_reference", ref_frame, "image/jpeg") if ref_frame else None)
    job = api(engine, "/v1/videos", body, ctype)
    jid = job.get("id") or die(f"submit failed: {job}")
    print(f"  job {jid} ({'fl2va from ' + os.path.basename(ref_frame) if ref_frame else 't2va'})")
    t0 = time.time()
    while True:
        time.sleep(POLL_S)
        j = api(engine, f"/v1/videos/{jid}")
        st = j.get("status")
        if st == "completed":
            print(f"  completed in {j.get('inference_time_s', 0):.0f}s")
            break
        if st not in ("queued", "in_progress"):
            die(f"job {jid} ended '{st}': {j.get('error')} — "
                "the engine likely needs a restart before retrying (#5793)")
        if time.time() - t0 > SHOT_TIMEOUT_S:
            die(f"job {jid} exceeded {SHOT_TIMEOUT_S}s")
    open(out_path, "wb").write(api(engine, f"/v1/videos/{jid}/content", raw=True))


def main():
    if len(sys.argv) != 2:
        die("usage: sequence.py <shots.json>")
    spec = json.load(open(sys.argv[1]))
    name = spec["name"]
    engine = spec.get("engine", "http://127.0.0.1:8091")
    dflt = {"width": 672, "height": 384, "steps": 30, "seed": None,
            "duration": 5.0} | spec.get("defaults", {})
    workdir = os.path.abspath(os.path.join("sequences", name))
    os.makedirs(workdir, exist_ok=True)

    clips = []
    prev_frame = None
    for i, shot in enumerate(spec["shots"], 1):
        clip = os.path.join(workdir, f"shot{i:02d}.mp4")
        if "use" in shot:
            if not os.path.exists(clip):
                shutil.copy(shot["use"], clip)
            print(f"shot {i}: existing clip {shot['use']}")
        elif os.path.exists(clip):
            print(f"shot {i}: already rendered, skipping")
        else:
            params = dflt | shot
            print(f"shot {i}: rendering — {shot['prompt'][:60]}…")
            render(engine, params, prev_frame, clip)
        frame = os.path.join(workdir, f"shot{i:02d}-last.jpg")
        ffmpeg(workdir, "-sseof", "-0.15", "-i", os.path.basename(clip),
               "-update", "1", "-frames:v", "1", "-q:v", "2",
               os.path.basename(frame))
        prev_frame = frame
        clips.append(clip)

    concat = os.path.join(workdir, "concat.txt")
    open(concat, "w").write(
        "".join(f"file '{os.path.basename(c)}'\n" for c in clips))
    final = os.path.join(workdir, "final.mp4")
    try:  # identical codec params from one engine — stream copy usually works
        ffmpeg(workdir, "-f", "concat", "-safe", "0", "-i", "concat.txt",
               "-c", "copy", "final.mp4")
    except subprocess.CalledProcessError:
        print("stream-copy concat refused; re-encoding")
        ffmpeg(workdir, "-f", "concat", "-safe", "0", "-i", "concat.txt",
               "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "final.mp4")
    print(f"final: {final}")


if __name__ == "__main__":
    main()
