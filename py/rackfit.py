"""Will a model fit? The arithmetic dgx-serve uses everywhere (rack fit,
rack recipes --<platform>). Stdlib only.

A platform's budget is what rack platform reports: unified memory on a DGX
Spark, the Metal working-set limit on a Mac, the sum of GPU memory on Linux
and Windows. From it, rack holds back a reserve for the system, and asks the
weights for headroom on top of their size (KV cache, activations, runtime).
"""

# GB the platform keeps for itself: a Spark's OS, monitor and page cache
# (rack up caps the engine container at total minus this), a discrete GPU's
# CUDA context, the Mac's display and window server.
RESERVE_GB = {"dgx": 9.0, "linux": 1.0, "windows": 1.5, "mac": 0.5}
HEADROOM = 1.15      # weights x this, plus FIXED_GB, is what serving needs
FIXED_GB = 1.0


def usable_gb(platform, budget_mb, nodes=1):
    """GB a model may use on `nodes` machines of this platform."""
    per_node = max(0.0, budget_mb / 1024.0 - RESERVE_GB.get(platform, 1.0))
    return per_node * max(1, nodes)


def needs_gb(weights_gb):
    return weights_gb * HEADROOM + FIXED_GB


def verdict(weights_gb, platform, budget_mb, nodes=1):
    """(fits, needs_gb, usable_gb); fits is None when the size is unknown."""
    usable = usable_gb(platform, budget_mb, nodes)
    if weights_gb is None:
        return None, None, usable
    need = needs_gb(weights_gb)
    return need <= usable, need, usable
