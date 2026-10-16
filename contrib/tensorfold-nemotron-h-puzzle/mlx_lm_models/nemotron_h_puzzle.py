# Nemotron-H "Puzzle" — heterogeneous per-layer MoE on top of nemotron_h.
#
# NVIDIA's NemotronH_Omni / Bolt models (model_type "nemotron_h_puzzle") are the
# base Nemotron-H hybrid Mamba-2 + attention + MoE stack, except each MoE layer
# has its OWN expert geometry: n_routed_experts, num_experts_per_tok and
# moe_intermediate_size vary layer to layer (config.block_configs[i]); mamba and
# attention blocks are uniform. This mirrors NVIDIA's HF modeling_nemotron_h_puzzle.py
# (a thin subclass that builds each block from config.get_nemotron_h_config_for_layer(i))
# for mlx_lm: build every block from a per-layer ModelArgs, and stack experts with
# the per-layer count when sanitizing weights.

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import mlx.core as mx
import mlx.nn as nn

from . import nemotron_h
from .nemotron_h import NemotronHBlock

# The only fields a per-layer block config overrides (NVIDIA's MoeConfig; attention
# and mamba blocks carry nothing beyond block_type).
_BLOCK_FIELDS = ("moe_intermediate_size", "num_experts_per_tok", "n_routed_experts")


@dataclass()
class ModelArgs(nemotron_h.ModelArgs):
    block_configs: Optional[List[Dict[str, Any]]] = None
    mtp_block_configs: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self):
        # Derive the block-type list from block_configs so the base normalizes it
        # into hybrid_override_pattern.
        if (self.block_configs and not self.layers_block_type
                and not self.layer_types and self.hybrid_override_pattern is None):
            self.layers_block_type = [b["block_type"] for b in self.block_configs]
        super().__post_init__()
        # n_routed_experts and moe_intermediate_size vary per MoE layer (read from
        # each layer's module), but num_experts_per_tok (top_k) is uniform. The
        # fused decode kernel reads a single global top_k, so surface it here.
        if self.num_experts_per_tok is None and self.block_configs:
            topks = {b.get("num_experts_per_tok") for b in self.block_configs
                     if b.get("block_type") == "moe" and b.get("num_experts_per_tok") is not None}
            if len(topks) == 1:
                self.num_experts_per_tok = topks.pop()

    def args_for_layer(self, i: int) -> "nemotron_h.ModelArgs":
        """A vanilla nemotron_h ModelArgs for layer i, with that layer's MoE geometry."""
        d = asdict(self)
        d.pop("block_configs", None)
        d.pop("mtp_block_configs", None)
        if self.block_configs and i < len(self.block_configs):
            bc = self.block_configs[i]
            for f in _BLOCK_FIELDS:
                if bc.get(f) is not None:
                    d[f] = bc[f]
        return nemotron_h.ModelArgs.from_dict(d)


class NemotronHModel(nemotron_h.NemotronHModel):
    def __init__(self, args: ModelArgs):
        nn.Module.__init__(self)
        self.embeddings = nn.Embedding(args.vocab_size, args.hidden_size)
        self.layers = [
            NemotronHBlock(args.args_for_layer(i), block_type)
            for i, block_type in enumerate(args.hybrid_override_pattern)
        ]
        self.norm_f = nn.RMSNorm(args.hidden_size, eps=args.layer_norm_epsilon)
        self.fa_idx = 0
        self.ssm_idx = 0
        for b in args.hybrid_override_pattern:
            if b == "*":
                break
            elif b == "M":
                self.fa_idx += 1
        for b in args.hybrid_override_pattern:
            if b == "*":
                self.ssm_idx += 1
            elif b == "M":
                break
    # __call__ is inherited from nemotron_h.NemotronHModel


class Model(nemotron_h.Model):
    def __init__(self, args: ModelArgs):
        nn.Module.__init__(self)
        self.args = args
        self.backbone = NemotronHModel(args)
        self.lm_head = nn.Linear(args.hidden_size, args.vocab_size, bias=False)
        self.model_type = args.model_type
    # __call__, layers, make_cache, cast_predicate are inherited

    def sanitize(self, weights):
        weights = {k: v for (k, v) in weights.items() if not k.startswith("mtp.")}
        for k, v in weights.items():
            if "conv1d.weight" in k and v.shape[-1] != 1:
                weights[k] = v.moveaxis(2, 1)
        # Stack per-expert weights into the SwitchMLP table using each layer's own
        # expert count (no-op when the checkpoint already ships stacked tables).
        for l in range(self.args.num_hidden_layers):
            prefix = f"backbone.layers.{l}.mixer"
            n_exp = self.args.args_for_layer(l).n_routed_experts or 0
            for m, n in [("down_proj", "fc2"), ("up_proj", "fc1")]:
                if f"{prefix}.experts.0.{m}.weight" in weights:
                    to_join = [
                        weights.pop(f"{prefix}.experts.{e}.{m}.weight")
                        for e in range(n_exp)
                    ]
                    weights[f"{prefix}.switch_mlp.{n}.weight"] = mx.stack(to_join)
        return weights
