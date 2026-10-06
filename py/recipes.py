"""Recipes v2: check, read and describe dgx-serve recipes. Stdlib only.

    python3 py/recipes.py list  [--json] [--platform P] [--here P] [--budget-mb N] < index
    python3 py/recipes.py check < index     exit 1 when any recipe has an error

The index comes from lib/recipe.sh, one recipe per line:
    name<TAB>location<TAB>source
location is a folder (recipes v2: model.env plus <platform>.env) or a flat
.env file (before 1.0: a vLLM container recipe for dgx and linux); source is
"mine" (~/.config/dgx-serve/recipes) or "repo".

Recipes are bash and rack sources them, so a recipe may only assign
variables and source its own model.env (or, flat, a parent recipe). Nothing
here sources a file until it, and everything it sources, passed that check.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rackfit  # noqa: E402

PLATFORMS = ["dgx", "linux", "windows", "mac"]
FLAT_PLATFORMS = ["dgx", "linux"]
ENGINES = ["vllm", "llamacpp"]
ROLES = ["chat", "tools", "reasoning", "vision", "code", "embedding", "rerank", "draft"]
SCALARS = ["MODEL", "MODEL_REVISION", "ENGINE", "IMAGE", "ARTIFACT", "ARTIFACT_REVISION", "ARTIFACT_MMPROJ",
           "ROLES", "WEIGHTS_GB", "SERVED_NAME", "GATEWAY_NAME"]
ARRAYS = ["SERVE_ARGS", "ENV_EXTRA", "MODS"]
OWNED_FLAGS = ["--host", "--port", "--api-key", "--api-key-file"]
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ------------------------------------------------------------------ check --
class Problems:
    def __init__(self):
        self.items = []          # (level, path, line, message)

    def add(self, item):
        if item not in self.items:          # a flat file is read once per platform
            self.items.append(item)

    def error(self, path, line, msg):
        self.add(("error", path, line, msg))

    def warn(self, path, line, msg):
        self.add(("warning", path, line, msg))

    def errors(self):
        return [p for p in self.items if p[0] == "error"]


def scan(text):
    """Split bash source into statements, as (line, words), honouring quotes,
    comments and multi-line arrays. Raises ValueError on anything a recipe
    must not contain: command or process substitution, backticks, chaining."""
    stmts, words, word, line_no = [], [], None, 1
    i, n = 0, len(text)
    depth = 0                       # inside NAME=( ... )
    start_line = 1

    def end_word():
        nonlocal word
        if word is not None:
            words.append(word)
            word = None

    def end_stmt():
        nonlocal words, start_line
        end_word()
        if words:
            stmts.append((start_line, words))
        words = []

    while i < n:
        c = text[i]
        if c == "\n":
            line_no += 1
            if depth == 0:
                end_stmt()
                start_line = line_no
            else:
                end_word()
            i += 1
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "#" and word is None:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise ValueError("line %d: unterminated single quote" % line_no)
            word = (word or "") + text[i:j + 1]
            line_no += text.count("\n", i, j)
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                elif text[j] == "`" or text.startswith("$(", j):
                    raise ValueError("line %d: command substitution inside double quotes" % (line_no + text.count("\n", i, j)))
                j += 1
            if j >= n:
                raise ValueError("line %d: unterminated double quote" % line_no)
            word = (word or "") + text[i:j + 1]
            line_no += text.count("\n", i, j)
            i = j + 1
            continue
        if c == "`" or text.startswith("$(", i) or text.startswith("<(", i) or text.startswith(">(", i):
            raise ValueError("line %d: command or process substitution (%s)" % (line_no, text[i:i + 2]))
        if c in ";|&<>":
            raise ValueError("line %d: '%s' (recipes assign variables; they do not run commands)" % (line_no, c))
        if c == "\\" and i + 1 < n:
            word = (word or "") + text[i:i + 2]
            if text[i + 1] == "\n":
                line_no += 1
                word = word[:-2] if word.endswith("\\\n") else word
            i += 2
            continue
        if c == "(":
            if word is not None and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=$", word):
                word += "("
                depth += 1
                i += 1
                end_word()
                continue
            raise ValueError("line %d: '(' outside an array assignment" % line_no)
        if c == ")":
            if depth == 0:
                raise ValueError("line %d: ')' without an array" % line_no)
            depth -= 1
            end_word()
            words.append(")")
            i += 1
            continue
        word = (word or "") + c
        i += 1
    if depth:
        raise ValueError("unterminated array")
    end_stmt()
    return stmts


def unquote(w):
    if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"":
        return w[1:-1]
    return w


def check_file(path, recipe_dir, root, problems, flat):
    """Lexical rules for one file. Returns the files it sources, resolved."""
    try:
        text = open(path).read()
    except OSError as e:
        problems.error(path, 0, "cannot read: %s" % e)
        return []
    try:
        stmts = scan(text)
    except ValueError as e:
        problems.error(path, 0, str(e))
        return []
    sources = []
    for line, words in stmts:
        head = words[0]
        if head in (".", "source"):
            if len(words) != 2:
                problems.error(path, line, "source exactly one file")
                continue
            target = unquote(words[1])
            if target.startswith("$RECIPE_DIR/") or target.startswith("${RECIPE_DIR}/"):
                rel = target.split("/", 1)[1]
                if "/" in rel or not rel.endswith(".env"):
                    problems.error(path, line, "only a file of this recipe's own folder may be sourced: %s" % target)
                    continue
                sources.append(os.path.join(recipe_dir, rel))
            elif re.match(r"^recipes/[A-Za-z0-9._/-]+\.env$", target) and ".." not in target:
                sources.append(os.path.join(root, target))
            else:
                problems.error(path, line, "sources %s: a recipe may source \"$RECIPE_DIR/model.env\" "
                                           "(or, flat, recipes/<parent>.env), nothing else" % target)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=", head)
        if not m:
            problems.error(path, line, "'%s' is a command: recipes only assign variables "
                                       "(and source model.env)" % head)
            continue
        if head.endswith("("):                       # NAME=( ... ) arrays
            if m.group(1) == "SERVE_ARGS" and os.path.basename(path) == "model.env":
                problems.warn(path, line, "SERVE_ARGS belong in the platform files, not model.env")
            continue
        # A=1 B=2 is two assignments; A=1 cmd would run cmd
        for w in words[1:]:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", w):
                problems.error(path, line, "'%s' after an assignment would run as a command" % w)
                break
    return sources


# ------------------------------------------------------------------- read --
DUMP = r'''
SERVE_ARGS=() ENV_EXTRA=() MODS=()
RECIPE_DIR=$2
cd "$3" || exit 3
. "$1" >/dev/null 2>&1 || exit 4
for v in %s $(compgen -v DIALECT_ || true); do
  if [ -n "${!v+x}" ]; then printf 'S\0%%s\0%%s\0' "$v" "${!v}"; fi
done
for a in %s; do
  eval "n=\${#$a[@]}"
  printf 'A\0%%s\0%%s\0' "$a" "$n"
  eval "for x in \${$a[@]+\"\${$a[@]}\"}; do printf '%%s\0' \"\$x\"; done"
done
''' % (" ".join(SCALARS), " ".join(ARRAYS))


def source(path, recipe_dir, root):
    """The variables a recipe file sets, by sourcing it in a clean bash."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"), "LANG": "C"}
    p = subprocess.run(["bash", "-c", DUMP, "_", path, recipe_dir, root], env=env,
                       capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError("sourcing %s failed (exit %d)" % (path, p.returncode))
    parts = p.stdout.decode("utf-8", "replace").split("\0")
    out, i = {}, 0
    while i < len(parts) - 1:
        kind = parts[i]
        if kind == "S":
            out[parts[i + 1]] = parts[i + 2]
            i += 3
        elif kind == "A":
            name, count = parts[i + 1], int(parts[i + 2])
            out[name] = parts[i + 3:i + 3 + count]
            i += 3 + count
        else:
            break
    return out


def human_int(v):
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)$", v or "")
    if not m:
        return None
    n = float(m.group(1))
    mult = {"": 1, "k": 1000, "K": 1024, "m": 1000 ** 2, "M": 1024 ** 2}[m.group(2)]
    return int(n * mult)


def flag(args, *names):
    """The value of the last of these flags (--flag v or --flag=v), or None;
    True for a flag given without a value."""
    val = None
    for i, a in enumerate(args):
        for nm in names:
            if a == nm:
                nxt = args[i + 1] if i + 1 < len(args) else None
                val = nxt if nxt is not None and not nxt.startswith("-") else True
            elif a.startswith(nm + "="):
                val = a.split("=", 1)[1]
    return val


def engine_of(v, platform, flat):
    """ENGINE, or vllm where a vLLM container is what rack always ran (flat
    recipes, and DGX or Linux variants that do not say)."""
    return v.get("ENGINE") or ("vllm" if flat or platform in FLAT_PLATFORMS else "")


def describe(v, platform, flat):
    """What a variant serves, read from its variables and SERVE_ARGS."""
    args = v.get("SERVE_ARGS", [])
    engine = engine_of(v, platform, flat)
    d = {"engine": engine or None, "image": v.get("IMAGE") or None,
         "artifact": v.get("ARTIFACT") or None, "artifact_revision": v.get("ARTIFACT_REVISION") or None,
         "context": None, "tensor_parallel": 1, "pipeline_parallel": 1, "nodes": 1,
         "tools": None, "reasoning": None, "vision": None, "speculative": None, "quantization": None}
    try:
        d["weights_gb"] = float(v["WEIGHTS_GB"]) if v.get("WEIGHTS_GB") else None
    except ValueError:
        d["weights_gb"] = None
    roles = (v.get("ROLES") or "").split()
    if engine == "llamacpp":
        ctx = flag(args, "--ctx-size", "-c")
        d["context"] = human_int(ctx) if isinstance(ctx, str) else None
        d["tools"] = "jinja" if flag(args, "--jinja") else None
        r = flag(args, "--reasoning-format")
        d["reasoning"] = r if isinstance(r, str) and r != "none" else None
        d["vision"] = bool(flag(args, "--mmproj") or v.get("ARTIFACT_MMPROJ")) or ("vision" in roles)
        draft = flag(args, "--model-draft", "-md")
        d["speculative"] = {"method": "draft", "model": draft} if isinstance(draft, str) else None
    else:
        ctx = flag(args, "--max-model-len")
        d["context"] = human_int(ctx) if isinstance(ctx, str) else None
        tp = flag(args, "--tensor-parallel-size", "-tp")
        pp = flag(args, "--pipeline-parallel-size", "-pp")
        d["tensor_parallel"] = int(tp) if isinstance(tp, str) and tp.isdigit() else 1
        d["pipeline_parallel"] = int(pp) if isinstance(pp, str) and pp.isdigit() else 1
        # one GPU per Spark; Linux boxes put tensor parallelism on their own GPUs
        d["nodes"] = d["tensor_parallel"] * d["pipeline_parallel"] if platform == "dgx" else d["pipeline_parallel"]
        if flag(args, "--enable-auto-tool-choice"):
            p = flag(args, "--tool-call-parser")
            d["tools"] = p if isinstance(p, str) else "auto"
        r = flag(args, "--reasoning-parser")
        d["reasoning"] = r if isinstance(r, str) else None
        mm = flag(args, "--limit-mm-per-prompt")
        if isinstance(mm, str):
            try:
                d["vision"] = int(json.loads(mm).get("image", 1)) > 0
            except (ValueError, AttributeError):
                d["vision"] = True
        else:
            d["vision"] = "vision" in roles
        spec = flag(args, "--speculative-config")
        if isinstance(spec, str):
            try:
                s = json.loads(spec)
                d["speculative"] = {"method": s.get("method"), "tokens": s.get("num_speculative_tokens")}
            except ValueError:
                d["speculative"] = {"method": "unknown"}
        q = flag(args, "--quantization", "-q")
        d["quantization"] = q if isinstance(q, str) else None
    return d


def semantic(v, platform, flat, path, problems):
    if not v.get("MODEL"):
        problems.error(path, 0, "no MODEL (it belongs in model.env)")
    engine = engine_of(v, platform, flat)
    if not flat:
        if engine not in ENGINES:
            problems.error(path, 0, "ENGINE must be vllm or llamacpp (got: %s)" % (engine or "nothing"))
        if platform == "mac" and engine != "llamacpp":
            problems.error(path, 0, "a Mac serves with llama.cpp (ENGINE=llamacpp)")
    if engine == "llamacpp":
        a = v.get("ARTIFACT", "")
        if "FILL_ME" not in a and not re.match(r"^[^/\s]+/[^/\s]+/.+\.gguf$", a):
            problems.error(path, 0, "llama.cpp needs ARTIFACT=<org>/<repo>/<file>.gguf (got: %s)" % (a or "nothing"))
    args = v.get("SERVE_ARGS", [])
    for f in OWNED_FLAGS:
        if flag(args, f) is not None:
            (problems.warn if flat else problems.error)(
                path, 0, "%s is the launcher's (rack up sets host, port and the API key)" % f)
    for r in (v.get("ROLES") or "").split():
        if r not in ROLES:
            problems.warn(path, 0, "unknown role %s (known: %s)" % (r, " ".join(ROLES)))
    if any("FILL_ME" in str(x) for x in list(v.values())):
        problems.warn(path, 0, "unanswered FILL_ME: rack up refuses it until the question is answered")
    if v.get("WEIGHTS_GB"):
        try:
            float(v["WEIGHTS_GB"])
        except ValueError:
            problems.error(path, 0, "WEIGHTS_GB is a number of GB (got: %s)" % v["WEIGHTS_GB"])


def variants_of(location):
    """[(platform, file, flat)] for a recipe location."""
    if location.endswith(".env"):
        return [(p, location, True) for p in FLAT_PLATFORMS]
    return [(p, os.path.join(location, p + ".env"), False) for p in PLATFORMS
            if os.path.exists(os.path.join(location, p + ".env"))]


def checked_closure(path, recipe_dir, root, problems, flat, seen=None):
    """Check a file and everything it sources; True when sourcing is safe."""
    seen = seen if seen is not None else set()
    if path in seen:
        return True
    seen.add(path)
    before = len(problems.errors())
    for s in check_file(path, recipe_dir, root, problems, flat):
        if not os.path.exists(s):
            problems.error(path, 0, "sources %s, which does not exist" % s)
            continue
        checked_closure(s, os.path.dirname(s) if not flat else recipe_dir, root, problems, flat, seen)
    return len(problems.errors()) == before


def read_recipe(name, location, source_kind, root, here=None, budget_mb=None, only=None):
    problems = Problems()
    flat = location.endswith(".env")
    rec = {"name": name, "source": source_kind, "layout": "flat" if flat else "v2", "location": location,
           "platforms": [], "model": None, "roles": [], "dialect": {}, "variants": {}}
    if not flat:
        checked_closure(os.path.join(location, "model.env"), location, root, problems, flat)
    shared = None
    for platform, f, is_flat in variants_of(location):
        if only and platform != only:
            continue
        recipe_dir = os.path.dirname(f)
        var = {"file": f}
        if not checked_closure(f, recipe_dir, root, problems, is_flat):
            var["error"] = "fails rack recipes check"
            rec["variants"][platform] = var
            rec["platforms"].append(platform)
            continue
        try:
            v = source(f, recipe_dir, root)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            problems.error(f, 0, str(e))
            var["error"] = str(e)
            rec["variants"][platform] = var
            rec["platforms"].append(platform)
            continue
        semantic(v, platform, is_flat, f, problems)
        var.update(describe(v, platform, is_flat))
        var["model"] = v.get("MODEL") or None
        if here == platform and budget_mb:
            fits, need, usable = rackfit.verdict(var.get("weights_gb"), platform, budget_mb, var["nodes"],
                                                 var.get("engine") or "vllm")
            var["fits"] = fits
            var["needs_gb"] = round(need, 1) if need else None
            var["usable_gb"] = round(usable, 1)
        else:
            var["fits"] = None
        rec["variants"][platform] = var
        rec["platforms"].append(platform)
        if shared is None:
            shared = v
    if shared:
        rec["model"] = shared.get("MODEL") or None
        rec["roles"] = (shared.get("ROLES") or "").split()
        rec["dialect"] = {k[len("DIALECT_"):].lower(): val for k, val in sorted(shared.items())
                          if k.startswith("DIALECT_")}
    rec["problems"] = [{"level": l, "file": p, "line": ln, "message": m} for l, p, ln, m in problems.items]
    return rec, problems


def pull_spec(file, recipe_dir, root, platform):
    """What `rack pull <recipe>` fetches for one variant: the repo, the
    revision, and either exact files (llama.cpp's GGUF) or vLLM's weights."""
    flat = os.path.basename(file) not in [p + ".env" for p in PLATFORMS]
    v = source(file, recipe_dir, root)
    engine = engine_of(v, platform, flat)
    d = describe(v, platform, flat)
    if engine == "llamacpp":
        a = v.get("ARTIFACT") or ""
        if not re.match(r"^[^/\s]+/[^/\s]+/.+\.gguf$", a):
            raise ValueError("ARTIFACT must be <org>/<repo>/<file>.gguf (got: %s)" % (a or "nothing"))
        org, repo, path = a.split("/", 2)
        rev = v.get("ARTIFACT_REVISION") or "main"
        specs = [{"repo": org + "/" + repo, "revision": rev, "files": [path]}]
        mm = v.get("ARTIFACT_MMPROJ")
        if mm:
            o2, r2, p2 = mm.split("/", 2)
            if o2 + "/" + r2 == specs[0]["repo"]:
                specs[0]["files"].append(p2)
            else:
                specs.append({"repo": o2 + "/" + r2, "revision": "main", "files": [p2]})
        return {"engine": engine, "pulls": specs, "model": v.get("MODEL") or None,
                "context": d["context"], "kv_fp8": False}
    model = v.get("MODEL") or ""
    if model.count("/") != 1 or model.startswith("/"):
        raise ValueError("MODEL is not a Hugging Face repo (%s): nothing to fetch" % (model or "nothing"))
    kv = flag(v.get("SERVE_ARGS", []), "--kv-cache-dtype")
    return {"engine": engine, "pulls": [{"repo": model, "revision": v.get("MODEL_REVISION") or "main", "weights": True}],
            "model": model, "context": d["context"], "kv_fp8": isinstance(kv, str) and kv.startswith("fp8")}


def read_index(stream):
    out = []
    for line in stream:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            out.append(parts)
    return out


def main(argv):
    import argparse
    if argv[:1] == ["pull-spec"]:                  # pull-spec <file> <recipe_dir> <root> <platform>
        try:
            print(json.dumps(pull_spec(*argv[1:5])))
            return 0
        except (ValueError, RuntimeError) as e:
            sys.stderr.write("%s\n" % e)
            return 1
    ap = argparse.ArgumentParser(prog="recipes.py")
    ap.add_argument("command", choices=["list", "check"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--platform")
    ap.add_argument("--here")
    ap.add_argument("--budget-mb", type=int)
    ap.add_argument("--root", default=os.getcwd())
    a = ap.parse_args(argv)
    index = read_index(sys.stdin)
    recs, all_problems = [], []
    for name, location, kind in index:
        only = a.platform if a.command == "check" else None
        rec, problems = read_recipe(name, location, kind, a.root, a.here, a.budget_mb, only)
        recs.append(rec)
        all_problems.extend(problems.items)

    if a.command == "check":
        errors = 0
        for level, path, line, msg in all_problems:
            rel = os.path.relpath(path, a.root) if path.startswith(a.root) else path
            print("%s%s: %s: %s" % (rel, ":%d" % line if line else "", level, msg))
            errors += level == "error"
        print("%d recipe%s, %d error%s, %d warning%s" % (
            len(recs), "" if len(recs) == 1 else "s", errors, "" if errors == 1 else "s",
            len(all_problems) - errors, "" if len(all_problems) - errors == 1 else "s"))
        return 1 if errors else 0

    if a.platform:
        recs = [r for r in recs if a.platform in r["platforms"]
                and r["variants"][a.platform].get("fits") is not False]
    if a.json:
        print(json.dumps({"schema": 1, "platform": a.platform, "here": a.here, "budget_mb": a.budget_mb,
                          "recipes": recs}, separators=(",", ":")))
        return 0
    if not recs:
        print("  none fit here" if a.platform and a.platform == a.here else "  none")
    for r in recs:
        plats = ",".join(r["platforms"])
        if a.platform:
            v = r["variants"][a.platform]
            what = (v.get("artifact") or "").rsplit("/", 1)[-1] or (r["model"] or "")
            size = "%.1f GB" % v["weights_gb"] if v.get("weights_gb") else "size ?"
            fit = {True: "fits", None: ""}.get(v.get("fits"), "")
            topo = "TP=%d" % v["tensor_parallel"] if v.get("tensor_parallel", 1) > 1 else "solo"
            print("  %-30s %-6s %-9s %-5s %s (%s)" % (r["name"], topo, size, fit, what, v.get("engine") or "?"))
        else:
            first = r["variants"][r["platforms"][0]] if r["platforms"] else {}
            topo = "TP=%d" % first["tensor_parallel"] if first.get("tensor_parallel", 1) > 1 else "solo"
            bad = " (rack recipes check)" if any(p["level"] == "error" for p in r["problems"]) else ""
            print("  %-30s %-6s %-24s %s%s" % (r["name"], topo, plats, r["model"] or "", bad))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
