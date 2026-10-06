"""rack pull's download half: run py/hfget.py for each pull of a spec
(py/recipes.py pull-spec), or show what it would fetch.

    python3 py/pull.py <spec-json> <plan 0|1> <json 0|1> <workers> <platform>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402
import hfget  # noqa: E402


def main(argv):
    spec, plan, as_json, workers, platform = json.loads(argv[0]), argv[1] == "1", argv[2] == "1", argv[3], argv[4]
    replicate = workers.split() if spec["engine"] == "vllm" and platform in ("dgx", "linux") else []
    out = []
    try:
        for p in spec["pulls"]:
            s = hfget.pull(p["repo"], p.get("revision", "main"), p.get("files"), p.get("weights", False),
                           cache=os.environ.get("HF_CACHE"), dry_run=plan, quiet=as_json)
            s["replicate_to"] = replicate
            out.append(s)
            if not plan:
                print("  %s: %s" % (p["repo"], s["snapshot"]))
    except (hfget.HubError, fetch.FetchError) as e:
        sys.stderr.write("\033[31m%s\033[0m\n" % e)
        return 1
    if plan and as_json:
        print(json.dumps({"schema": 1, "command": "pull", "platform": platform, "engine": spec["engine"], "pulls": out}))
    elif plan:
        print("\033[1mplan: rack pull for %s (nothing was run)\033[0m" % platform)
        for s in out:
            print("  %s@%s  %d files, %s; %s to download, %s free at %s" % (
                s["repo"], s["commit"][:7], len(s["files"]), fetch.human(s["bytes"]), fetch.human(s["to_download"]),
                fetch.human(s["free"]), s["cache"]))
            if s["replicate_to"]:
                print("  then replicate to %s and verify the shards on every node" % ", ".join(s["replicate_to"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
