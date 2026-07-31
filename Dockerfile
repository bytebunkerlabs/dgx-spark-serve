# dgx-spark-serve — one image, both nodes. Build ON spark-1 (aarch64), then
# scripts/build.sh syncs it to the worker byte-identically.
#
# Two base profiles (scripts/build.sh picks via --profile):
#
#   ngc       nvcr.io/nvidia/vllm:26.07-py3
#             NVIDIA's blessed Spark image: native arm64, CUDA 13.3.1, GB10-tuned
#             FlashInfer/CUTLASS NVFP4 paths. Ships vLLM 0.24.0 — fine for
#             phases 1–2, CANNOT serve Inkling (needs >= 0.26.0).
#
#   upstream  vllm/vllm-openai:v0.26.0-aarch64-cu129-ubuntu2404
#             First-party upstream arm64 image with Inkling support and native
#             multi-node serve flags (--nnodes/--node-rank/--headless).
#             Caveats: cu129 build on a CUDA-13 platform (works via driver
#             compat; NVFP4 kernel coverage on sm_121 must be smoke-tested),
#             and its pip-bundled NCCL causes multi-node hangs on Spark
#             (vllm#42354) — the guard below fixes that.
#
# Phase 3 reality check: even v0.26.0 cannot boot Inkling on GB10 unpatched —
# the sm_12x FA4 fix (vllm#49681) was closed unmerged. The patch is applied at
# launch time as a mod (scripts/fetch-inkling-mod.sh), not baked in here, so
# the image stays model-agnostic and the mod is deletable when upstream lands
# Dao-AILab/flash-attention#2348.

ARG BASE_IMAGE=nvcr.io/nvidia/vllm:26.07-py3
FROM ${BASE_IMAGE}

# --- NCCL redirect guard (no-op on NGC) --------------------------------------
# On pip-wheel bases the bundled NCCL hangs multi-node on Spark (vllm#42354).
# If a system libnccl exists, point the pip copy at it. NGC images manage their
# own NCCL and have no pip copy, so this does nothing there.
RUN set -e; \
    sys=/usr/lib/aarch64-linux-gnu/libnccl.so.2; \
    if [ -e "$sys" ]; then \
      for f in /usr/local/lib/python3*/dist-packages/nvidia/nccl/lib/libnccl.so.2 \
               /usr/local/lib/python3*/site-packages/nvidia/nccl/lib/libnccl.so.2; do \
        if [ -e "$f" ]; then ln -sf "$sys" "$f" && echo "nccl: redirected $f -> $sys"; fi; \
      done; \
    fi; true

# --- optional fast loaders (upstream profile) --------------------------------
# instanttensor is the community loader the Inkling recipe leans on: a 171 GB
# checkpoint against ~110 GB usable per node makes fastsafetensors unsafe
# (documented OOM risk above 0.85 of RAM). Torch is pinned first so pip cannot
# swap the CUDA build for a CPU wheel while resolving deps.
ARG INSTALL_EXTRAS=0
RUN set -e; \
    if [ "$INSTALL_EXTRAS" = "1" ]; then \
      tv=$(python3 -c "import torch; print(torch.__version__)"); \
      printf 'torch==%s\n' "$tv" > /tmp/torch-pin.txt; \
      # scipy: Inkling's vision tower imports linear_sum_assignment; the
      # official image does not ship it (fails at load with ModuleNotFoundError)
      pip install --no-cache-dir --constraint /tmp/torch-pin.txt scipy; \
      pip install --no-cache-dir --constraint /tmp/torch-pin.txt \
        fastsafetensors instanttensor \
        || echo "extras unavailable on this arch — default loader will be used"; \
    fi

LABEL org.bytebunkerlabs.project="dgx-spark-serve"
