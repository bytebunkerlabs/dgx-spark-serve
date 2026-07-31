#!/usr/bin/env bash
# Fetch the Inkling sm_12x FA4 patch into mods/, pinned, with provenance.
#
# Why this exists: Inkling's FA4 attention cannot boot on GB10 — vLLM's pinned
# flash-attn has no SM120 paged-KV path ("Paged KV not supported on SM 12.0"),
# and the upstream fixes are unmerged (Dao-AILab/flash-attention#2348 open,
# vllm#49681 closed-unmerged). The working patch is eugr/spark-vllm-docker's
# mods/inkling-sm12-paged-kv (MIT), which vendors the BSD-3 FA4-SM120 bundle
# (HF SecondNatureComputing/flash-attn-4-sm120 @ 6011704) and applies three
# AST-validated, idempotent edits to one file inside the container at launch.
#
# We consume it as an upstream artifact rather than rewriting kernel plumbing:
# pinned commit, licenses retained, provenance recorded. DELETE this mod when
# flash-attention#2348 reaches vLLM's pinned FA4 fork.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO=https://github.com/eugr/spark-vllm-docker
PIN=${1:-main}   # pass a commit SHA to pin harder; recorded in PROVENANCE
DEST=mods/inkling-sm12-paged-kv

[ -d "$DEST" ] && { echo "$DEST already exists — rm it first to re-fetch" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "== fetching $REPO @ $PIN (sparse: mods/inkling-sm12-paged-kv)"
git clone --quiet --filter=blob:none --no-checkout "$REPO" "$tmp/repo"
git -C "$tmp/repo" sparse-checkout set mods/inkling-sm12-paged-kv
git -C "$tmp/repo" checkout --quiet "$PIN"
sha=$(git -C "$tmp/repo" rev-parse HEAD)

# the mod must be self-validating: run.sh + the vendored bundle + its license
for f in run.sh patch_inkling.py adapter.py vendor/inkling_sm120_fa4/LICENSE; do
  [ -e "$tmp/repo/mods/inkling-sm12-paged-kv/$f" ] \
    || { echo "upstream layout changed: missing $f — read the mod README before proceeding" >&2; exit 1; }
done

mkdir -p mods
cp -R "$tmp/repo/mods/inkling-sm12-paged-kv" "$DEST"
cat > "$DEST/PROVENANCE" <<EOF
fetched: $(date -u +%Y-%m-%dT%H:%M:%SZ)
source:  $REPO (MIT)
commit:  $sha
vendored FA4 bundle: HF SecondNatureComputing/flash-attn-4-sm120 (BSD-3-Clause)
  pinned upstream: see vendor/inkling_sm120_fa4/UPSTREAM_COMMIT
delete-when: Dao-AILab/flash-attention#2348 lands in vLLM's pinned FA4 fork
EOF
echo "== vendored at $DEST (commit $sha). Licenses retained; see PROVENANCE."
