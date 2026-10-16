"""Nemotron-H Puzzle (heterogeneous per-layer MoE: Bolt-74B-A9B, Super-VL).

The decode kernels, MTP verification and quant checks are the same as the
nemotron_h family; only the model geometry differs, and that lives in mlx_lm's
nemotron_h_puzzle module (each MoE layer built at its own expert count). So this
family reuses nemotron_h's loader and kernels and only claims the new model_type.
"""

from __future__ import annotations

from tensorfold.families import nemotron_h as _base

MODEL_TYPES = ("nemotron_h_puzzle",)
TITLE = "Nemotron 3.5 Bolt"
LANES = _base.LANES
MODELS = ("rapid-mlx/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-MLX-4bit",)
REQUIRED_FILES: dict = {}
KERNEL_PACKAGE = _base.KERNEL_PACKAGE
KERNEL_VERSION = _base.KERNEL_VERSION
KERNEL_DEPENDENCIES = _base.KERNEL_DEPENDENCIES
MLX_ENV = _base.MLX_ENV
GROUPS = _base.GROUPS

# reused unchanged from nemotron_h (quant gate, loader, lane-engine settings)
refusal = _base.refusal
check = _base.check
unreadable = _base.unreadable
load = _base.load
engine_settings = _base.engine_settings
