#!/usr/bin/env bash
# Every place a video generation leaves a trace on a node. Read-only by
# default: it tells you what exists and how big. `--delete` removes it.
#
#   rack scrub              inventory, both nodes, touches nothing
#   rack scrub --delete     remove generated media, prompts, and run logs
#
# Written for handing the boxes to someone else: a tenant's prompts and
# renders should not outlive their session, and neither should yours.
# Deliberately NOT deleted: recipes, scripts, docs, the model cache, or
# anything git-tracked in the repo — those are the rack, not the tenancy.
set -uo pipefail
cd "$(dirname "$0")/.."

DEL=0
[ "${1:-}" = "--delete" ] && DEL=1
say() { printf '  %s\n' "$*"; }
sz() { du -sh "$1" 2>/dev/null | cut -f1; }

# 1. render workdirs — the clips and the extracted frames
found=0
for d in sequences/*/; do
  [ -d "$d" ] || continue
  found=1
  say "render dir  $d ($(sz "$d"), $(ls "$d" | wc -l) files)"
  [ "$DEL" = 1 ] && rm -rf "$d" && say "            deleted"
done
[ "$found" = 0 ] && say "render dirs: none"

# 2. shot lists — these hold the prompts. Git-tracked ones are the rack's
# own examples; untracked ones belong to whoever wrote them.
for f in sequences/*.json; do
  [ -e "$f" ] || continue
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    say "shot list   $f (git-tracked — kept)"
  else
    say "shot list   $f (untracked, has prompts)"
    [ "$DEL" = 1 ] && rm -f "$f" && say "            deleted"
  fi
done

# 3. sequencer run logs — prompts are echoed into these
for f in /tmp/*.log; do
  [ -e "$f" ] || continue
  grep -qE "job video_gen_|scene[0-9]+ shot" "$f" 2>/dev/null || continue
  say "run log     $f ($(sz "$f"))"
  [ "$DEL" = 1 ] && rm -f "$f" && say "            deleted"
done

# 4. loose media outside the repo — the home dir AND /tmp, which is where
# clips really pile up (downloads, poster frames, benchmark output).
mapfile -t loose < <(find "$HOME" /tmp -maxdepth 6 \
  \( -name "*.mp4" -o -name "*-last.jpg" -o -name "*-poster.jpg" \) \
  -not -path "*/dgx-spark-serve/sequences/*" 2>/dev/null)
if [ ${#loose[@]} -gt 0 ]; then
  say "loose media ${#loose[@]} file(s) outside the repo:"
  printf '              %s\n' "${loose[@]:0:8}"
  [ ${#loose[@]} -gt 8 ] && say "              ... and $(( ${#loose[@]} - 8 )) more"
  if [ "$DEL" = 1 ]; then
    printf '%s\0' "${loose[@]}" | xargs -0 rm -f && say "            deleted"
  fi
else
  say "loose media: none"
fi

# 5. the running engine's job store — jobs live in the container, so a
# restart clears them; while it is up, ask the API to drop each one.
for port in 8091; do
  ids=$(curl -s -m 5 "http://127.0.0.1:$port/v1/videos" 2>/dev/null \
    | python3 -c "import json,sys
try: print(' '.join(j['id'] for j in json.load(sys.stdin).get('data',[])))
except Exception: pass" 2>/dev/null)
  [ -z "$ids" ] && continue
  n=$(echo "$ids" | wc -w)
  say "engine jobs  $n on :$port (clips + prompts, in-container)"
  if [ "$DEL" = 1 ]; then
    for id in $ids; do curl -s -m 10 -X DELETE "http://127.0.0.1:$port/v1/videos/$id" >/dev/null; done
    say "            deleted (a rack down also clears these)"
  fi
done

# 6. journald keeps engine logs after the container is gone. Prompts are not
# logged by the engine (checked: only status lines and tracebacks), but the
# job ids are — and vacuuming needs root, so it stays a manual step.
if command -v journalctl >/dev/null; then
  n=$(journalctl CONTAINER_NAME=serve_solo --since "7 days ago" 2>/dev/null | wc -l)
  say "journald     $n engine log lines (no prompt text; clear with:"
  say "             sudo journalctl --rotate --vacuum-time=1s)"
fi

[ "$DEL" = 1 ] && say "scrub complete" || say "read-only inventory — add --delete to remove"
exit 0
