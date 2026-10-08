"""Which GPU this machine has, for picking a PyTorch build (baitbook/comfyui.py).

`BAITBOOK_GPU` overrides it: nvidia, amd, intel, mps (Apple silicon) or cpu.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

KINDS = ("nvidia", "amd", "intel", "mps", "cpu")
# AMD architectures by product name, for AMD's per-GPU PyTorch packages on Windows
# (the device-* extras; the ComfyUI README's table plus neighbours). No match: device-all.
AMD_ARCH = [
    (r"\b(8060S|8050S|8040S)\b|Ryzen AI Max", "gfx1151"),
    (r"\b(890M|880M)\b", "gfx1150"),
    (r"\b(780M|760M|740M)\b", "gfx1103"),
    (r"RX 9070|AI PRO R9700", "gfx1201"),
    (r"RX 9060", "gfx1200"),
    (r"RX 7900|PRO W7900|PRO W7800", "gfx1100"),
    (r"RX 7[78]00", "gfx1101"),
    (r"RX 7600|PRO W7600", "gfx1102"),
    (r"RX 6[89][05]0", "gfx1030"),
    (r"RX 67[05]0", "gfx1031"),
    (r"RX 66[05]0", "gfx1032"),
]


@dataclass
class Gpu:
    kind: str                   # one of KINDS
    name: str = ""              # what the system calls it
    cuda: tuple[int, int] | None = None     # NVIDIA: the highest CUDA version the driver supports
    compute: tuple[int, int] | None = None  # NVIDIA: compute capability
    arch: str | None = None     # AMD: gfx target, when known

    def __str__(self) -> str:
        extra = f", CUDA {self.cuda[0]}.{self.cuda[1]}" if self.cuda else f", {self.arch}" if self.arch else ""
        return f"{self.kind} ({self.name or 'unnamed'}{extra})"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _version(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m[1]), int(m[2])) if m else None


def _nvidia() -> Gpu | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"]).strip().splitlines()
    if not out:
        return None
    name, _, cap = out[0].rpartition(",")
    cuda = re.search(r"CUDA Version:\s*([\d.]+)", _run(["nvidia-smi"]))
    return Gpu("nvidia", name.strip(), cuda=_version(cuda[1]) if cuda else None, compute=_version(cap))


def _adapter_names() -> list[str]:
    """The display adapters' names (Windows, Linux) or [] if they can't be found."""
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"])
        return [line.strip() for line in out.splitlines() if line.strip()]
    if shutil.which("lspci"):
        return [line for line in _run(["lspci"]).splitlines() if re.search(r"VGA|3D|Display", line)]
    vendors = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
    return [vendors.get(p.read_text().strip(), "") for p in Path("/sys/class/drm").glob("card*/device/vendor")]


def amd_arch(name: str) -> str | None:
    return next((arch for pattern, arch in AMD_ARCH if re.search(pattern, name, re.I)), None)


def forced() -> str | None:
    """BAITBOOK_GPU as a kind. It also takes the install scripts' names for the language
    model's build (cuda, cu124, hip, hip-radeon, rocm72, metal); vulkan says nothing about the vendor."""
    v = (os.environ.get("BAITBOOK_GPU") or "").lower()
    if v in KINDS:
        return v
    if v == "cuda" or re.fullmatch(r"cu\d+", v):
        return "nvidia"
    if v.startswith(("hip", "rocm")):
        return "amd"
    return {"metal": "mps", "xpu": "intel"}.get(v)


def detect() -> Gpu:
    if kind := forced():
        found = detect_hardware()
        return found if found.kind == kind else Gpu(kind, "set by BAITBOOK_GPU")
    return detect_hardware()


def detect_hardware() -> Gpu:
    if sys.platform == "darwin":
        return Gpu("mps", "Apple silicon") if platform.machine() == "arm64" else Gpu("cpu", "Intel Mac")
    if gpu := _nvidia():
        return gpu
    names = _adapter_names()
    if amd := next((n for n in names if re.search(r"\b(AMD|Radeon)\b", n, re.I)), None):
        return Gpu("amd", amd, arch=amd_arch(amd))
    if intel := next((n for n in names if re.search(r"\bArc\b", n)), None):
        return Gpu("intel", intel)
    return Gpu("cpu", names[0] if names else "")
