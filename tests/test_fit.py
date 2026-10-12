"""py/fit.py: the KV cache per model architecture.

    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "py"))
import fit  # noqa: E402

DENSE = {"num_hidden_layers": 36, "num_attention_heads": 32, "num_key_value_heads": 8, "head_dim": 128}
PER_LAYER = 2 * 8 * 128 * 2


class KVCache(unittest.TestCase):
    def test_dense_counts_every_layer(self):
        self.assertEqual(fit.kv_cache(DENSE), (36 * PER_LAYER, 0))
        self.assertEqual(fit.kv_bytes_per_token(DENSE), 36 * PER_LAYER)

    def test_sliding_layers_keep_their_window(self):
        cfg = dict(DENSE, sliding_window=1024,
                   layer_types=["sliding_attention"] * 30 + ["full_attention"] * 6)
        self.assertEqual(fit.kv_cache(cfg), (6 * PER_LAYER, 30 * 1024 * PER_LAYER))

    def test_linear_layers_do_not_grow(self):
        cfg = dict(DENSE, layer_types=["linear_attention", "linear_attention", "linear_attention",
                                       "full_attention"] * 9)
        self.assertEqual(fit.kv_cache(cfg), (9 * PER_LAYER, 0))
        self.assertEqual(fit.kv_cache(dict(DENSE, full_attention_interval=4)), (9 * PER_LAYER, 0))

    def test_text_config_and_mla(self):
        self.assertEqual(fit.kv_cache({"text_config": DENSE})[0], 36 * PER_LAYER)
        mla = {"num_hidden_layers": 10, "kv_lora_rank": 512, "qk_rope_head_dim": 64}
        self.assertEqual(fit.kv_cache(mla), (10 * 576 * 2, 0))

    def test_unknown(self):
        self.assertEqual(fit.kv_cache(None), (None, 0))
        self.assertEqual(fit.kv_cache({"num_hidden_layers": 4}), (None, 0))

    def test_fixed_part_counts_against_the_window(self):
        kv, _ = fit.assess(10e9, PER_LAYER, 32768, None, "mac", 24576, 1, "llamacpp")
        kv2, v2 = fit.assess(10e9, PER_LAYER, 32768, None, "mac", 24576, 1, "llamacpp", kv_fixed=2e9)
        self.assertAlmostEqual(kv2 - kv, 2.0)
        _, v = fit.assess(10e9, PER_LAYER, 32768, None, "mac", 24576, 1, "llamacpp")
        self.assertLess(v2[0]["max_context"], v[0]["max_context"])


if __name__ == "__main__":
    unittest.main()
