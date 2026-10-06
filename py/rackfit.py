"""Will a model fit? The arithmetic dgx-serve uses everywhere (rack fit,
rack recipes --<platform>). Stdlib only.

A platform's budget is what rack platform reports: unified memory on a DGX
Spark, the Metal working-set limit on a Mac, the sum of GPU memory on Linux
and Windows. From it, rack keeps a reserve for the system, and serving needs
the weights, the KV cache for the context window, and the engine's own
working memory (activations, CUDA graphs, compute buffers).
"""

# GB the platform keeps for itself: a Spark's OS, monitor and page cache
# (rack up caps the engine container at total minus this), a discrete GPU's
# CUDA context and, on Windows, the desktop; the Mac's window server.
RESERVE_GB = {"dgx": 9.0, "linux": 0.5, "windows": 0.8, "mac": 0.5}
# The engine's own memory on top of weights and KV, and how much bigger the
# weights get once loaded (vLLM pads and keeps a few buffers; a GGUF loads as is).
OVERHEAD_GB = {"vllm": 1.5, "llamacpp": 0.5}
WEIGHT_FACTOR = {"vllm": 1.03, "llamacpp": 1.0}
KV_GUESS = 0.10            # KV as a share of the weights, when the config is unknown
MIN_CONTEXT = 4096         # a window too small to be useful does not count as fitting


def usable_gb(platform, budget_mb, nodes=1):
    """GB a model may use on `nodes` machines of this platform."""
    per_node = max(0.0, budget_mb / 1024.0 - RESERVE_GB.get(platform, 1.0))
    return per_node * max(1, nodes)


def fixed_gb(weights_gb, engine="vllm", nodes=1):
    """Weights as loaded, plus the engine's memory on every node."""
    return weights_gb * WEIGHT_FACTOR.get(engine, 1.03) + OVERHEAD_GB.get(engine, 1.5) * max(1, nodes)


def needs_gb(weights_gb, engine="vllm", kv_gb=None, nodes=1):
    kv = kv_gb if kv_gb is not None else weights_gb * KV_GUESS
    return fixed_gb(weights_gb, engine, nodes) + kv


def max_context(weights_gb, kv_bytes_per_token, platform, budget_mb, engine="vllm", nodes=1):
    """The longest window whose KV cache still fits beside the weights, or None."""
    if not kv_bytes_per_token:
        return None
    room = usable_gb(platform, budget_mb, nodes) - fixed_gb(weights_gb, engine, nodes)
    return max(0, int(room * 1e9 // kv_bytes_per_token))


def verdict(weights_gb, platform, budget_mb, nodes=1, engine="vllm", kv_gb=None):
    """(fits, needs_gb, usable_gb); fits is None when the size is unknown."""
    usable = usable_gb(platform, budget_mb, nodes)
    if weights_gb is None:
        return None, None, usable
    need = needs_gb(weights_gb, engine, kv_gb, nodes)
    return need <= usable, need, usable
