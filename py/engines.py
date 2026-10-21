"""The engines rack runs, pinned. Stdlib only.

vLLM runs as a container (DGX Spark, NVIDIA Linux) or, on Windows without
Docker, from a venv. llama.cpp runs natively from one numbered release build
of ggml-org/llama.cpp, verified by sha256 before it is unpacked: Metal on a
Mac, CUDA on Linux and in WSL2 (with the matching cudart bundle, since WSL2
has the driver but not the CUDA runtime libraries). TensorFold runs natively
on a Mac from its own venv, its source pinned to one commit.
"""
import os
import shutil
import subprocess
import tarfile

VLLM_IMAGE = {"dgx": "dgx-spark-serve:dev", "linux": "vllm/vllm-openai:v0.26.0"}
VLLM_VERSION = "0.26.0"          # the venv's vLLM, matching the image above

LLAMACPP_BUILD = "b11675"        # 2026-10-16 (adds NemotronHPuzzle load support)
LLAMACPP_URL = "https://github.com/ggml-org/llama.cpp/releases/download/%s/%%s" % LLAMACPP_BUILD
# variant -> [(asset, sha256)], the GitHub release's own digests
LLAMACPP_ASSETS = {
    "macos-arm64": [
        ("llama-b11675-bin-macos-arm64.tar.gz",
         "4a2c2a12a9061f5daeef79b5a0e0d172c4872e4f039828ad24a6eb4f0f9b3fa4")],
    "cuda-12.8-x64": [
        ("llama-b11675-bin-ubuntu-cuda-12.8-x64.tar.gz",
         "5968303a119823017ec54e6448c5443bb4a9b97526ac617317c7ef7d9445b97c"),
        ("cudart-llama-b11675-bin-ubuntu-cuda-12.8-x64.tar.gz",
         "f8aa995a09bf67d2f19a6489ce856c9d51b1773e0bc515ed7271b67c982795cd")],
    "cuda-13.4-x64": [
        ("llama-b11675-bin-ubuntu-cuda-13.4-x64.tar.gz",
         "e68773e1aeff0b2b325814d4349cacdc26022fa90ba6680de11b8818104ae5fd"),
        ("cudart-llama-b11675-bin-ubuntu-cuda-13.4-x64.tar.gz",
         "ba40ab0f97380065bb77cb39a400fda0b7e91aa618eefb46887d8fca0031915c")],
    "cuda-13.4-arm64": [
        ("llama-b11675-bin-ubuntu-cuda-13.4-arm64.tar.gz",
         "410209bb3e9300e0e68db55bb90a2aca7f7c4e53a5162bd2479fd7df85627d34"),
        ("cudart-llama-b11675-bin-ubuntu-cuda-13.4-arm64.tar.gz",
         "184a5cd33c3ea71eafdc570c476071d498380e337e2b7cb963b253576b8f5ed0")],
}


# TensorFold (github.com/ashhart/TensorFold), Apache-2.0: MLX on a Mac, with
# lossless speculative decoding from the checkpoint's MTP layer. 0.6.6 is its
# last Python engine and the last that serves Qwen3.6 MoE (qwen3_5_moe); the
# native 1.0 engine refuses that model type. Audited at this commit
# (docs/12-platforms.md, "TensorFold"): no telemetry; the only outbound calls
# are a release check (off: --no-update-check, TENSORFOLD_NO_UPDATE_CHECK=1)
# and Hugging Face downloads for a repo not in the cache (off: HF_HUB_OFFLINE=1).
TENSORFOLD_VERSION = "0.6.6"
TENSORFOLD_COMMIT = "cb2ebf0540f42604e2759b2ddef497861e928248"      # tag v0.6.6, 2026-10-06
TENSORFOLD_PACKAGE = "tensorfold @ git+https://github.com/ashhart/TensorFold.git@" + TENSORFOLD_COMMIT
TENSORFOLD_PYTHON = (3, 11)       # its pyproject's requires-python


class EngineError(Exception):
    pass


def _version(v):
    try:
        return tuple(int(x) for x in (v or "").split(".")[:2])
    except ValueError:
        return ()


def llamacpp_variant(facts):
    """Which build serves this machine: by OS, CPU and the driver's CUDA."""
    os_, arch = facts.get("os"), facts.get("arch")
    if os_ == "darwin":
        if arch == "arm64":
            return "macos-arm64"
        raise EngineError("llama.cpp with Metal needs an Apple Silicon Mac")
    cuda = _version(facts.get("cuda"))
    if not cuda:
        raise EngineError("no CUDA driver found (nvidia-smi): llama.cpp's CUDA build needs one")
    if arch in ("aarch64", "arm64"):
        if cuda >= (13, 0):
            return "cuda-13.4-arm64"
        raise EngineError("llama.cpp for arm64 Linux needs a CUDA 13 driver (this one is %s)" % facts.get("cuda"))
    if cuda >= (13, 0):
        return "cuda-13.4-x64"
    if cuda >= (12, 8):
        return "cuda-12.8-x64"
    raise EngineError("llama.cpp's CUDA build needs a driver with CUDA 12.8 or newer (this one is %s): "
                      "update the NVIDIA driver%s" % (facts.get("cuda"), " on Windows" if facts.get("wsl") else ""))


def llamacpp_dir(state, variant):
    return os.path.join(state, "engines", "llama.cpp", "%s-%s" % (LLAMACPP_BUILD, variant))


def llamacpp_server(state, variant):
    return os.path.join(llamacpp_dir(state, variant), "llama-%s" % LLAMACPP_BUILD, "llama-server")


def llamacpp_libpath(state, variant):
    """Directories for LD_LIBRARY_PATH (empty on a Mac)."""
    if variant.startswith("macos"):
        return []
    d = llamacpp_dir(state, variant)
    return [os.path.join(d, "llama-%s" % LLAMACPP_BUILD)] + [
        os.path.join(d, name[:-len(".tar.gz")]) for name, _ in LLAMACPP_ASSETS[variant] if name.startswith("cudart-")]


def safe_extract(tar_path, dest):
    """Unpack a tarball, refusing any member that would land outside dest."""
    dest = os.path.realpath(dest)
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == dest or target.startswith(dest + os.sep)):
                raise EngineError("%s: member %s escapes the install directory" % (tar_path, m.name))
            if m.issym() or m.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
                if not link.startswith(dest + os.sep):
                    raise EngineError("%s: link %s points outside the install directory" % (tar_path, m.name))
            if m.isdev():
                raise EngineError("%s: device file %s" % (tar_path, m.name))
        t.extractall(dest)


def tensorfold_dir(state):
    return os.path.join(state, "engines", "tensorfold", TENSORFOLD_COMMIT)


def tensorfold_bin(state):
    return os.path.join(tensorfold_dir(state), "bin", "tensorfold")


def python_at_least(minimum, which=shutil.which, run=subprocess.run):
    """A Python this new for a venv: python3.13..python3.11 on PATH, else one uv
    has (installing it with uv when uv is there). None when there is none."""
    def ok(exe):
        try:
            r = run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=30)
            return r.returncode == 0 and tuple(int(x) for x in r.stdout.strip().split(".")) >= minimum
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
    for minor in range(14, minimum[1] - 1, -1):
        exe = which("python3.%d" % minor)
        if exe and ok(exe):
            return exe
    uv = which("uv")
    if uv:
        want = ">=%d.%d" % minimum
        for args in (["python", "find", want], ["python", "install", "%d.%d" % (minimum[0], max(minimum[1], 12))]):
            try:
                r = run([uv] + args, capture_output=True, text=True, timeout=900)
            except (OSError, subprocess.SubprocessError):
                return None
            if args[1] == "install" and r.returncode == 0:
                r = run([uv, "python", "find", want], capture_output=True, text=True, timeout=60)
            exe = (r.stdout or "").strip().splitlines()[-1:] if r.returncode == 0 else []
            if exe and ok(exe[0]):
                return exe[0]
    return None
