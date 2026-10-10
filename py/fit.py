"""rack fit: will this model fit this machine? Stdlib only.

    python3 py/fit.py --platform P --budget-mb N [--nodes K] (--repo ORG/NAME | --spec JSON) [--json]

The numbers come from the hub: the files the platform would download (the
same selection rack pull makes: vLLM's safetensors, or one GGUF file), the
parameter count, and config.json for the KV cache per token. The budget is
this machine's, as rack platform reports it: unified memory on a DGX Spark
(times the Sparks in the rack), the Metal working-set limit on a Mac, GPU
memory on Linux and Windows. py/rackfit.py holds the reserve per platform.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402
import hfget  # noqa: E402
import rackfit  # noqa: E402
import term  # noqa: E402

DEFAULT_CONTEXT = 32768
PLATFORM_BUDGET = {"dgx": "unified memory", "mac": "the Metal working-set limit", "linux": "GPU memory",
                   "windows": "GPU memory"}
PLATFORM_NAME = {"dgx": "DGX Spark", "mac": "Mac", "linux": "Linux machine", "windows": "Windows PC"}


def revision_info(repo, revision, tok):
    """The revision API's full answer (listing, parameters, GGUF metadata)."""
    url = "%s/api/models/%s/revision/%s?blobs=true" % (hfget.endpoint(), repo, urllib.parse.quote(revision, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with fetch.opener().open(req, timeout=60) as r:
            return json.load(r)
    except Exception:
        hfget.listing(repo, revision, tok)        # raises the right HubError
        raise


def config_of(repo, revision, tok):
    url = "%s/%s/resolve/%s/config.json" % (hfget.endpoint(), repo, urllib.parse.quote(revision, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with fetch.opener().open(req, timeout=30) as r:
            return json.load(r)
    except Exception:
        return None


def kv_bytes_per_token(cfg, dtype_bytes=2):
    """KV cache bytes per token, from config.json (MLA models store a latent)."""
    if not cfg:
        return None
    t = cfg.get("text_config") or cfg
    layers = t.get("num_hidden_layers")
    if not layers:
        return None
    if t.get("kv_lora_rank"):
        return int(layers * (t["kv_lora_rank"] + (t.get("qk_rope_head_dim") or 0)) * dtype_bytes)
    heads = t.get("num_attention_heads")
    kv = t.get("num_key_value_heads") or heads
    head_dim = t.get("head_dim") or (t.get("hidden_size") // heads if heads and t.get("hidden_size") else None)
    if not (kv and head_dim):
        return None
    return int(2 * layers * kv * head_dim * dtype_bytes)


def active_params(cfg):
    """Parameters per token in the experts of a mixture of experts, roughly."""
    t = (cfg or {}).get("text_config") or cfg or {}
    routed = t.get("n_routed_experts") or t.get("num_experts") or t.get("num_local_experts")
    hidden, layers = t.get("hidden_size"), t.get("num_hidden_layers")
    if not (routed and hidden and layers):
        return None
    top = t.get("num_experts_per_tok") or t.get("num_experts_per_token") or 2
    shared = t.get("n_shared_experts") or t.get("num_shared_experts") or 0
    inter = t.get("moe_intermediate_size") or t.get("intermediate_size")
    if not inter:
        return None
    moe_layers = layers - (t.get("first_k_dense_replace") or 0)
    return {"active": (top + shared) * 3 * hidden * inter * moe_layers, "routed": routed, "top": top, "shared": shared}


def assess(weights_bytes, kv_per_token, context, native, platform, budget_mb, nodes, engine):
    """Per node count: what serving needs at `context` (a recipe's window; for
    a bare repo, the smallest useful one), and the longest window that fits."""
    gb = weights_bytes / 1e9
    ctx = context or rackfit.MIN_CONTEXT
    kv_gb = (kv_per_token * ctx / 1e9) if kv_per_token else None
    out = []
    for n in sorted({1, nodes}):
        fits, need, usable = rackfit.verdict(gb, platform, budget_mb, n, engine, kv_gb)
        mc = rackfit.max_context(gb, kv_per_token, platform, budget_mb, engine, n)
        if mc is not None and native:
            mc = min(mc, native)
        out.append({"nodes": n, "needs_gb": round(need, 1), "usable_gb": round(usable, 1), "fits": fits,
                    "max_context": mc})
    return kv_gb, out


def fit(platform, budget_mb, nodes, repo=None, spec=None):
    tok = hfget.token()
    res = {"schema": 1, "platform": platform, "budget_mb": budget_mb, "nodes": nodes,
           "budget_is": PLATFORM_BUDGET.get(platform, "memory")}
    if spec:
        pulls = spec["pulls"]
        res["recipe"] = spec.get("recipe")
        context = spec.get("context")
        kv_dtype = 1 if spec.get("kv_fp8") else 2
    else:
        pulls = [{"repo": repo, "revision": "main", "weights": True}]
        context, kv_dtype = None, 2
    p0 = pulls[0]
    info = revision_info(p0["repo"], p0.get("revision", "main"), tok)
    commit, listed = hfget.listing(p0["repo"], p0.get("revision", "main"), tok)
    ggufs = [f for f in listed if f["path"].endswith(".gguf") and "mmproj" not in f["path"].lower()]
    res["repo"], res["commit"] = p0["repo"], commit
    base = (spec or {}).get("model") or ((info.get("cardData") or {}).get("base_model") if ggufs else None)
    if isinstance(base, list):
        base = base[0] if base else None
    cfg = config_of(p0["repo"], p0.get("revision", "main"), tok) or (config_of(base, "main", tok) if base else None)
    params = (info.get("safetensors") or {}).get("total") or (info.get("gguf") or {}).get("total")
    res["parameters"] = params
    act = active_params(cfg)
    if act:
        res["active_parameters"] = act["active"]
        res["experts"] = "%d routed + %d shared of %d" % (act["top"], act["shared"], act["routed"])
    native = ((cfg or {}).get("text_config") or cfg or {}).get("max_position_embeddings") \
        or (info.get("gguf") or {}).get("context_length")
    res["native_context"] = native
    res["context"] = context                 # None for a bare repo: the verdict says how long a window fits
    kvt = kv_bytes_per_token(cfg, kv_dtype)
    res["kv_bytes_per_token"] = kvt
    engine = (spec or {}).get("engine") or ("llamacpp" if ggufs and platform in ("mac", "windows") else "vllm")
    res["engine"] = engine

    if spec or not ggufs or (platform in ("dgx", "linux") and any(f["path"].endswith(".safetensors") for f in listed)):
        files = hfget.select(listed, p0.get("files"), p0.get("weights", False))
        size = sum(f["size"] or 0 for f in files)
        for extra in pulls[1:]:
            _, more = hfget.listing(extra["repo"], extra.get("revision", "main"), tok)
            size += sum(f["size"] or 0 for f in hfget.select(more, extra.get("files"), extra.get("weights", False)))
        res["files"] = [f["path"] for f in files]
        res["download_bytes"] = size
        if engine == "llamacpp" and spec is None:
            engine = res["engine"] = "vllm"   # a repo of safetensors serves with vLLM
        res["kv_gb"], res["verdicts"] = assess(size, kvt, context, native, platform, budget_mb, nodes, engine)
    else:                                       # a GGUF repo: every quant, smallest first
        res["quants"] = []
        for f in sorted(ggufs, key=lambda x: x["size"] or 0):
            kv_gb, v = assess(f["size"] or 0, kvt, context, native, platform, budget_mb, 1, "llamacpp")
            res["quants"].append({"file": f["path"], "bytes": f["size"], "needs_gb": v[0]["needs_gb"],
                                  "fits": v[0]["fits"], "max_context": v[0]["max_context"]})
            res["kv_gb"] = kv_gb
        res["usable_gb"] = round(rackfit.usable_gb(platform, budget_mb, 1), 1)
    return res


def show(r):
    w = sys.stdout.write
    title = r["repo"] + ("  (recipe %s)" % r["recipe"] if r.get("recipe") else "")
    w(term.paint(title, "1") + "\n")
    if r.get("download_bytes") is not None:
        n = len(r["files"])
        w("  download    %s in %d file%s\n" % (fetch.human(r["download_bytes"]), n, "" if n == 1 else "s"))
    if r.get("parameters"):
        w("  parameters  %.1f B\n" % (r["parameters"] / 1e9))
    if r.get("active_parameters"):
        w("  active      ~%.1f B per token in the experts (%s)\n" % (r["active_parameters"] / 1e9, r["experts"]))
    if r.get("kv_bytes_per_token"):
        if r.get("context"):
            w("  KV cache    %s for the recipe's %s tokens (%d KiB per token)\n" % (
                fetch.human(r["kv_bytes_per_token"] * r["context"]), "{:,}".format(r["context"]),
                r["kv_bytes_per_token"] // 1024))
        else:
            w("  KV cache    %d KiB per token%s\n" % (r["kv_bytes_per_token"] // 1024,
                                                    "; native window %s" % "{:,}".format(r["native_context"])
                                                    if r.get("native_context") else ""))
    w("  here        about %d GiB of %s on this %s%s\n" % (
        (r["budget_mb"] + 512) // 1024, r["budget_is"], PLATFORM_NAME.get(r["platform"], "machine"),
        ", %d in the rack" % r["nodes"] if r["nodes"] > 1 else ""))
    for v in r.get("verdicts", []):
        w("  %d node%s     needs ~%.1f GB of %.1f usable  ->  %s%s\n" % (
            v["nodes"], "s" if v["nodes"] > 1 else " ", v["needs_gb"], v["usable_gb"],
            "fits" if v["fits"] else "does not fit", window(v)))
    if r.get("quants"):
        w("  GGUF files (%.1f GB usable here):\n" % r["usable_gb"])
        for q in r["quants"]:
            w("    %-40s %9s  %s%s\n" % (q["file"], fetch.human(q["bytes"] or 0),
                                         "fits" if q["fits"] else "does not fit", window(q)))
    w("  more at https://fits.bytebunkerlabs.ai\n")


def window(v):
    mc = v.get("max_context")
    if mc is None or not v.get("fits"):
        return ""
    return ", up to %s tokens of context" % ("{:,}".format(mc) if mc < 10000 else "%dk" % (mc // 1000))


def main(argv):
    ap = argparse.ArgumentParser(prog="fit.py")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--budget-mb", type=int, required=True)
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--repo")
    ap.add_argument("--spec")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = fit(a.platform, a.budget_mb, a.nodes, a.repo, json.loads(a.spec) if a.spec else None)
    except (hfget.HubError, fetch.FetchError) as e:
        sys.stderr.write(term.paint(e, "31", sys.stderr) + "\n")
        return 1
    if a.json:
        print(json.dumps(r))
    else:
        show(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
