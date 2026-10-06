"""rack gateway: keep a LiteLLM config's routes to rack's engines current.
Stdlib only; no YAML library, on purpose.

    python3 py/gateway.py status <config>
    python3 py/gateway.py sync   <config> <name> <served-name> <api-base> <api-key-ref>
    python3 py/gateway.py remove <config> <name>
    python3 py/gateway.py adopt  <config> <name>

A LiteLLM config is hand-kept, with comments; a YAML round trip would
flatten them. So rack edits only between two marker lines inside
model_list, and leaves every other line exactly as it was:

  model_list:
    # >>> dgx-serve managed: rack up and rack down keep the routes in here
    - model_name: qwen3-8b
      ...
    # <<< dgx-serve managed

A name that also appears outside the markers is a conflict: rack refuses
to shadow a route someone wrote by hand. `adopt` moves such an entry in,
only when asked. Every write keeps the previous file as <config>.bak.
"""
import json
import os
import re
import sys

BEGIN = "# >>> dgx-serve managed: rack up and rack down keep the routes in here"
END = "# <<< dgx-serve managed"
ITEM = re.compile(r"^(\s*)-\s*model_name:\s*['\"]?([^'\"#\s]+)['\"]?\s*(#.*)?$")


class GatewayError(Exception):
    pass


def load(path):
    try:
        with open(path) as f:
            return f.read().split("\n")
    except OSError as e:
        raise GatewayError("cannot read the gateway config %s: %s" % (path, e))


def save(path, lines):
    text = "\n".join(lines)
    with open(path + ".bak", "w") as f:
        f.write(open(path).read())
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def model_list(lines):
    for i, ln in enumerate(lines):
        if re.match(r"^model_list:\s*(#.*)?$", ln):
            return i
    raise GatewayError("no top-level model_list: in the config")


def list_indent(lines, start):
    """The indentation of model_list's items (two spaces when it has none)."""
    for ln in lines[start + 1:]:
        if ln and not ln.startswith((" ", "\t", "#")):
            break
        m = ITEM.match(ln)
        if m:
            return m.group(1)
    return "  "


def block(lines):
    """(begin, end) line numbers of the managed block, or None."""
    b = [i for i, ln in enumerate(lines) if ln.strip() == BEGIN]
    e = [i for i, ln in enumerate(lines) if ln.strip() == END]
    if not b and not e:
        return None
    if len(b) != 1 or len(e) != 1 or e[0] < b[0]:
        raise GatewayError("the dgx-serve markers are damaged (one begin, one end, in that order): fix them by hand")
    return b[0], e[0]


def items(lines, lo, hi):
    """[(name, first line, last line)] of the list items in lines[lo:hi]. An
    item runs until a non-blank line indented no deeper than its dash."""
    out, i = [], lo
    while i < hi:
        m = ITEM.match(lines[i])
        if not m:
            i += 1
            continue
        ind, start, last, j = len(m.group(1)), i, i, i + 1
        while j < hi:
            ln = lines[j]
            if ln.strip():
                if len(ln) - len(ln.lstrip()) <= ind:
                    break
                last = j
            j += 1
        out.append((m.group(2), start, last))
        i = j
    return out


def section_end(lines, start):
    """The line after model_list's last item."""
    end = start + 1
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln and not ln.startswith((" ", "\t", "#")):
            break
        if ln.strip():
            end = i + 1
    return end


def ensure_block(lines):
    b = block(lines)
    if b:
        return lines, b
    start = model_list(lines)
    ind = list_indent(lines, start)
    lines = lines[:start + 1] + [ind + BEGIN, ind + END] + lines[start + 1:]
    return lines, (start + 1, start + 2)


def outside(lines, b):
    start = model_list(lines)
    end = section_end(lines, start)
    found = items(lines, start + 1, end)
    if b:
        found = [x for x in found if not (b[0] < x[1] < b[1])]
    return found


def render(ind, name, served, api_base, api_key):
    return [ind + "- model_name: " + name,
            ind + "  litellm_params:",
            ind + "    model: openai/" + served,
            ind + "    api_base: " + api_base,
            ind + "    api_key: " + api_key]


def status(path):
    lines = load(path)
    b = block(lines)
    managed = [x[0] for x in items(lines, b[0] + 1, b[1])] if b else []
    hand = [x[0] for x in outside(lines, b)]
    return {"config": path, "managed": managed, "by_hand": hand, "markers": bool(b)}


def sync(path, name, served, api_base, api_key):
    lines = load(path)
    lines, b = ensure_block(lines)
    if any(x[0] == name for x in outside(lines, b)):
        raise GatewayError("%s is already a route in %s, written by hand: rack gateway adopt %s moves it under "
                           "rack's care, or give the recipe another GATEWAY_NAME" % (name, path, name))
    ind = re.match(r"^(\s*)", lines[b[0]]).group(1)
    new = render(ind, name, served, api_base, api_key)
    mine = [x for x in items(lines, b[0] + 1, b[1]) if x[0] == name]
    if mine:
        _, lo, hi = mine[0]
        if lines[lo:hi + 1] == new:
            return "unchanged"
        lines = lines[:lo] + new + lines[hi + 1:]
    else:
        lines = lines[:b[1]] + new + lines[b[1]:]
    save(path, lines)
    return "changed"


def remove(path, name):
    lines = load(path)
    b = block(lines)
    if not b:
        return "unchanged"
    mine = [x for x in items(lines, b[0] + 1, b[1]) if x[0] == name]
    if not mine:
        return "unchanged"
    _, lo, hi = mine[0]
    save(path, lines[:lo] + lines[hi + 1:])
    return "changed"


def adopt(path, name):
    lines = load(path)
    lines, b = ensure_block(lines)
    hand = [x for x in outside(lines, b) if x[0] == name]
    if not hand:
        raise GatewayError("no route called %s outside rack's markers in %s" % (name, path))
    _, lo, hi = hand[0]
    entry = lines[lo:hi + 1]
    rest = lines[:lo] + lines[hi + 1:]
    b2 = block(rest)
    save(path, rest[:b2[1]] + entry + rest[b2[1]:])
    return "changed"


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    try:
        if cmd == "status":
            print(json.dumps(status(path)))
        elif cmd == "sync" and len(argv) == 6:
            print(sync(path, *argv[2:6]))
        elif cmd == "remove" and len(argv) == 3:
            print(remove(path, argv[2]))
        elif cmd == "adopt" and len(argv) == 3:
            print(adopt(path, argv[2]))
        else:
            sys.stderr.write(__doc__)
            return 2
    except GatewayError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
