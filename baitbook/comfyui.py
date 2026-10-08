"""baitbook's own ComfyUI: a pinned release, run headless by the Angler, in a Python 3.13
environment of its own with the PyTorch build for this machine's GPU (the commands in
ComfyUI's README). `baitbook comfyui` installs or repairs it; `baitbook setup` offers it.

  <home>/comfyui/
    ComfyUI-<version>/                 the code, from the release's zip on GitHub
    .venv/                             Python, PyTorch, ComfyUI's requirements
    models/ custom_nodes/ user/ ...    everything else (--base-directory), so a new
                                       version swaps the code and keeps these

A ComfyUI you installed yourself works too: point [image] comfy_dir / comfy_python at it.
"""

from __future__ import annotations

import io
import shutil
import socket
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from .gpu import Gpu, detect

VERSION = "0.39.0"  # tested with baitbook's workflow; bump with care
CODE_ZIP = "https://github.com/Comfy-Org/ComfyUI/archive/refs/tags/v{version}.zip"
GGUF_NODES = "https://github.com/city96/ComfyUI-GGUF/archive/6ea2651e7df66d7585f6ffee804b20e92fb38b8a.zip"  # for .gguf models
PYTHON = "3.13"  # what AMD's Windows wheels need; ComfyUI supports it well everywhere
PYTORCH = "https://download.pytorch.org/whl"
AMD_WINDOWS = "https://stable.repo.amd.com/rocm/whl-next/"
MODEL_FOLDERS = ("diffusion_models", "text_encoders", "vae")


class InstallError(RuntimeError):
    pass


def root() -> Path:
    from angler import config

    return config.HOME / "comfyui"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def models_dir(image: dict) -> Path | None:
    """Where ComfyUI looks for models: under [image] comfy_base, or in comfy_dir."""
    from angler import config

    base, code = config.path(image.get("comfy_base")), config.path(image.get("comfy_dir"))
    return base / "models" if base else code / "models" if code else None


def torch_args(gpu: Gpu) -> list[str]:
    """`uv pip install` arguments for PyTorch on this GPU, as ComfyUI's README gives them."""
    plain = ["torch", "torchvision", "torchaudio"]
    if gpu.kind == "nvidia":
        if gpu.compute and gpu.compute < (7, 5):
            index = "cu126"  # 10 series and older: the last CUDA build that has their kernels
        elif gpu.cuda is None or gpu.cuda >= (13, 0):
            index = "cu130"
        elif gpu.cuda >= (12, 8):
            index = "cu128"
        elif gpu.cuda >= (12, 6):
            index = "cu126"
        else:
            raise InstallError(f"The NVIDIA driver supports CUDA {gpu.cuda[0]}.{gpu.cuda[1]}; PyTorch needs 12.6 or newer. Update the driver and run this again.")
        return [*plain, "--extra-index-url", f"{PYTORCH}/{index}"]
    if gpu.kind == "amd" and sys.platform == "win32":
        device = f"device-{gpu.arch or 'all'}"  # just this GPU's kernels when it's known
        return ["--index-url", AMD_WINDOWS, f"torch[{device}]==2.13.0+rocm10.0.0",
                f"torchvision[{device}]==0.28.0+rocm10.0.0", "torchaudio==2.11.0.2+rocm10.0.0"]
    if gpu.kind == "amd":
        return [*plain, "--index-url", f"{PYTORCH}/rocm7.2"]
    if gpu.kind == "intel":
        return [*plain, "--index-url", f"{PYTORCH}/xpu"]
    if gpu.kind == "mps" or sys.platform == "darwin":
        return plain
    return [*plain, "--index-url", f"{PYTORCH}/cpu"]


def find_uv() -> str:
    """uv: on PATH (the install scripts put it there), or the one the `uv` package ships."""
    if found := shutil.which("uv"):
        return found
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except (ImportError, FileNotFoundError):
        raise InstallError("uv isn't installed (https://docs.astral.sh/uv/); the install scripts install it.") from None


def run(cmd: list[str], what: str) -> None:
    print(f"  > {what}", flush=True)
    if subprocess.run(cmd, check=False).returncode:
        raise InstallError(f"{what} failed (the output above says why). Run `baitbook comfyui` again to retry.")


def download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except OSError as exc:  # URLError, a timeout, a dropped connection
        raise InstallError(f"Couldn't download {url} ({exc}). Check the connection and run `baitbook comfyui` again.") from None


def unzip(data: bytes, into: Path) -> Path:
    """Extract a GitHub archive zip into `into`; returns its single top folder."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():  # nothing may land outside `into`
            if not (into / name).resolve().is_relative_to(into.resolve()):
                raise InstallError(f"unsafe path in the zip: {name}")
        z.extractall(into)
        return into / z.namelist()[0].split("/")[0]


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def comfy_url(current: str) -> str:
    """comfy_url for baitbook's own ComfyUI: the current one if its port is free, else the
    first free port from 8190. Never ComfyUI's default 8188, which a ComfyUI of your own
    (the desktop app, say) is likely to be using."""
    port = int(current.rsplit(":", 1)[-1]) if current.rsplit(":", 1)[-1].isdigit() else 8188
    if port != 8188 and port_free(port):
        return f"http://127.0.0.1:{port}"
    port = next((p for p in range(8190, 8300) if port_free(p)), None)
    if port is None:
        raise InstallError("no free port between 8190 and 8299 for ComfyUI; set [image] comfy_url by hand.")
    return f"http://127.0.0.1:{port}"


def needs_gguf(image: dict) -> bool:
    return any(str(image.get(k, "")).lower().endswith(".gguf") for k in ("unet", "text_encoder"))


def gpu_check(python: Path) -> tuple[bool, str]:
    """Does PyTorch in this environment see a GPU? (True/False, what it says)."""
    code = ("import torch; a = torch.cuda.is_available(); m = getattr(torch.backends, 'mps', None); m = bool(m and m.is_available());"
            "x = bool(getattr(torch, 'xpu', None) and torch.xpu.is_available());"
            "print(a or m or x, torch.__version__, torch.cuda.get_device_name(0) if a else 'mps' if m else 'xpu' if x else 'cpu')")
    out = subprocess.run([str(python), "-c", code], capture_output=True, text=True, check=False)
    if out.returncode:
        return False, (out.stderr.strip().splitlines() or ["PyTorch doesn't import"])[-1]
    ok, _, rest = out.stdout.strip().partition(" ")
    return ok == "True", rest


def install(gpu: Gpu | None = None, torch_again: bool = False) -> Path:
    """Install (or repair) baitbook's ComfyUI and point the config at it. Safe to run again:
    each step is skipped when it's done. Returns the ComfyUI code folder."""
    from angler import config

    config.load()
    gpu = gpu or detect()
    home, uv = root(), find_uv()
    code, venv = home / f"ComfyUI-{VERSION}", home / ".venv"
    python = venv_python(venv)
    print(f"Installing ComfyUI {VERSION} for baitbook in {home}\n  GPU: {gpu}")
    home.mkdir(parents=True, exist_ok=True)

    if not (code / "main.py").is_file():
        print(f"  > downloading ComfyUI {VERSION}", flush=True)
        top = unzip(download(CODE_ZIP.format(version=VERSION)), home)
        if top != code:
            shutil.rmtree(code, ignore_errors=True)
            top.rename(code)
    if not python.is_file():
        run([uv, "venv", "--no-project", "--python", PYTHON, str(venv)], f"a Python {PYTHON} environment")
    ok, said = gpu_check(python) if not torch_again else (False, "")
    if torch_again or "PyTorch doesn't import" in said or "No module named" in said:
        run([uv, "pip", "install", "--python", str(python), *(["--reinstall"] if torch_again else []), *torch_args(gpu)], "PyTorch for this GPU")
    run([uv, "pip", "install", "--python", str(python), "-r", str(code / "requirements.txt")], "ComfyUI's requirements")

    for folder in ("custom_nodes", *(f"models/{f}" for f in MODEL_FOLDERS)):
        (home / folder).mkdir(parents=True, exist_ok=True)
    if needs_gguf(config.section("image")):
        install_gguf_nodes(uv, python, home)
    for old in home.glob("ComfyUI-*"):  # earlier versions' code; models and nodes stay
        if old != code and (old / "main.py").is_file():
            shutil.rmtree(old, ignore_errors=True)

    url = comfy_url(config.section("image").get("comfy_url", ""))
    config.update({"image": {"comfy_dir": str(code), "comfy_python": str(python), "comfy_base": str(home), "comfy_url": url}}, local=True)
    ok, said = gpu_check(python)
    if ok:
        print(f"  ok  PyTorch {said}")
    else:
        print(f"  !!  PyTorch doesn't see a GPU ({said}). ComfyUI will run on the CPU, which is very slow for klein. "
              "Set BAITBOOK_GPU and run `baitbook comfyui --torch` to try another build.")
    print(f"ComfyUI is ready: {code}")
    return code


def install_gguf_nodes(uv: str, python: Path, home: Path) -> None:
    """ComfyUI-GGUF, for .gguf image models (klein 9B quantised, say)."""
    nodes = home / "custom_nodes" / "ComfyUI-GGUF"
    if not (nodes / "__init__.py").is_file():
        print("  > downloading the ComfyUI-GGUF nodes (for .gguf models)", flush=True)
        top = unzip(download(GGUF_NODES), home / "custom_nodes")
        shutil.rmtree(nodes, ignore_errors=True)
        top.rename(nodes)
    run([uv, "pip", "install", "--python", str(python), "-r", str(nodes / "requirements.txt")], "the ComfyUI-GGUF nodes' requirements")
