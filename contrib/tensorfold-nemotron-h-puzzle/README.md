# TensorFold / mlx_lm support for `nemotron_h_puzzle`

A day-one port so Apple Silicon can serve NVIDIA's **Nemotron-3.5 Bolt-74B-A9B**
(and the Super-VL family), whose `model_type` is `nemotron_h_puzzle` — a
heterogeneous "Puzzle" MoE that neither stock mlx_lm 0.32.0 nor TensorFold
cb2ebf0 handled. Both files are **additive** (no edits to existing code).

## What Puzzle adds over `nemotron_h`
Each MoE layer has its own geometry — `n_routed_experts` (240/304/368/432/496),
`moe_intermediate_size` (1536/2048/2304/2560) — while `num_experts_per_tok`
(top-k = 9) is uniform. Mamba and attention blocks are uniform. The model also
uses a LatentMoE (experts run at `moe_latent_size=1024`, projected from/to
`hidden_size=4096`) plus one shared expert. Mirrors NVIDIA's HF
`modeling_nemotron_h_puzzle.py` (each block built from its per-layer config).

## Files
- `mlx_lm_models/nemotron_h_puzzle.py` → `mlx_lm/models/nemotron_h_puzzle.py`
  (the model: per-layer block args, per-layer expert stacking in `sanitize`,
  surfaces the uniform top-k globally for the fused kernel).
- `tensorfold_families/nemotron_h_puzzle/__init__.py` →
  `tensorfold/families/nemotron_h_puzzle/__init__.py` (registers the model_type;
  reuses nemotron_h's quant gate / loader / lane kernels).

Install = copy each into the engine venv's package, matching paths.

## Status (measured 2026-10-15, M3 Max 128 GB, rapid-mlx 4-bit)
- **Plain mlx_lm: WORKS.** ~44 tok/s decode, 476 tok/s prefill @5k, peak 43.6 GB
  (51.9 GB @5k ctx). Coherent, correct output. (NVFP4 GGUF on llama.cpp b11675
  was ~8.5 tok/s — this is ~5x.)
- **TensorFold fused engine: WORKS** (with the two patches here). The
  `nemotron.lightning.v1` fused decode had no LatentMoE path (fed hidden 4096
  straight to the experts → warmup matmul 1024 vs 4096). The patches
  (`fused-latentmoe-kernels.patch`, `fused-latentmoe-model.patch`) wrap the
  expert call with `fc1_latent_proj` before and `fc2_latent_proj` after (weights
  sum to 1 under norm_topk_prob), keeping `shared_experts` on hidden — in both the
  backbone `_moe_block`/`_moe` and the MTP `_head_rows`. All guarded by
  `moe_latent_size`, so the Lightning (non-latent) path is byte-for-byte
  unchanged. Measured: serves correctly at **~45 tok/s single / ~51 tok/s at 4
  streams** on an M3 Max. `rack up nemotron-3.5-bolt-tf --mac` now works.
- **MTP speculative decode: not yet** — rapid-mlx ships no MTP head
  (`mtp-4bit.safetensors`), so it runs "one token a round" (no draft speedup).
  Getting MTP needs the head extracted from the BF16 model and quantized to MLX
  4-bit in TensorFold's layout — the remaining lever for a higher number.

Upstream target: github.com/ashhart/TensorFold (and mlx_lm). This dir is the
tracked copy; the live code is in the engine venv.
