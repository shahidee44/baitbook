"""`baitbook setup`: check that everything the Angler needs is in place, offer to install
baitbook's own ComfyUI (`baitbook comfyui`) and download the default models (`baitbook
models`), ask for the paths still missing, and add the app icon. `--check` only checks
(for scripts and CI): no questions, nothing installed or downloaded, and exit status 1
if anything is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

GEMMA = "https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf"
KLEIN_4B = "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B"
KLEIN_4B_EXTRAS = "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b"
# where the default files come from, by file name
GET = {
    "model": f"gemma-4-31B_q4_0-it.gguf (17.7 GB) from {GEMMA}",
    "mmproj": f"gemma-4-31B-it-mmproj.gguf (1.2 GB) from {GEMMA}",
    "flux-2-klein-4b.safetensors": f"{KLEIN_4B} (7.8 GB), into ComfyUI/models/diffusion_models",
    "qwen_3_4b.safetensors": f"{KLEIN_4B_EXTRAS}, split_files/text_encoders (8 GB), into ComfyUI/models/text_encoders",
    "flux2-vae.safetensors": f"{KLEIN_4B_EXTRAS}, split_files/vae (0.3 GB), into ComfyUI/models/vae",
}
FOLDERS = {"unet": ("diffusion_models", "unet"), "text_encoder": ("text_encoders", "clip"), "vae": ("vae",)}


def say(ok: bool | None, text: str) -> None:
    print(f"  {'ok' if ok else '--' if ok is None else '!!'}  {text}")


def ask_path(prompt: str, valid) -> Path | None:
    """A path typed in (quotes and ~ are fine) that passes valid(), or None for Enter."""
    while True:
        raw = input(f"      {prompt} (Enter to skip): ").strip().strip('"\'')
        if not raw:
            return None
        p = Path(raw).expanduser().resolve()
        if valid(p):
            return p
        print("      not found there; try again")


def yes(prompt: str) -> bool:
    return input(f"      {prompt} [Y/n]: ").strip().lower() in ("", "y", "yes")


def check_llama() -> int:
    try:
        import llama_cpp
    except ImportError:
        say(False, "llama-cpp-python isn't installed. Run the install script again (README, Install), which "
                   "picks the build for your GPU.")
        return 1
    gpu = getattr(llama_cpp, "llama_supports_gpu_offload", lambda: False)()
    say(True, f"llama-cpp-python {llama_cpp.__version__} ({'GPU' if gpu else 'CPU only: slow for a 31B model'})")
    return 0


def check_text(config, ask: bool) -> int:
    missing = 0
    for key, what in (("model", "language model GGUF"), ("mmproj", "vision projector (mmproj) GGUF")):
        p = config.path(config.section("text").get(key))
        if p and p.is_file():
            say(True, f"[text] {key}: {p}")
            continue
        say(False, f"[text] {key}: the {what} isn't set, or isn't there. `baitbook models` downloads {GET[key]}")
        p = ask_path(f"path to the {what}", Path.is_file) if ask else None
        if p:
            config.update({"text": {key: str(p)}}, local=True)
            say(True, f"[text] {key}: {p}")
        else:
            missing += 1
    return missing


def comfy_python_near(comfy: Path) -> Path | None:
    """ComfyUI's own Python: a venv inside it, or the portable build's embedded one."""
    for p in (comfy / "venv", comfy / ".venv", comfy.parent / "venv", comfy.parent / "python_embeded"):
        for exe in (p / "Scripts" / "python.exe", p / "bin" / "python", p / "python.exe"):
            if exe.is_file():
                return exe
    return None


def check_comfy(config, ask: bool) -> int:
    from angler.imagegen import ComfyClient

    image = config.section("image")
    comfy, python = config.path(image.get("comfy_dir")), config.path(image.get("comfy_python"))
    if comfy and (comfy / "main.py").is_file() and python and python.is_file():
        say(True, f"ComfyUI: {comfy}")
        return 0
    if ComfyClient(image).alive():
        say(None, f"ComfyUI is running at {image['comfy_url']}, but [image] comfy_dir / comfy_python aren't set, "
                  "so the Angler can't start it when it isn't.")
        return 0
    from . import comfyui

    say(False, "ComfyUI: none set up yet. `baitbook comfyui` installs one for baitbook (about 6 GB with PyTorch, in "
               f"{comfyui.root()}), or point [image] comfy_dir / comfy_python at a ComfyUI you have.")
    if not ask:
        return 1
    if yes(f"install ComfyUI {comfyui.VERSION} for baitbook now?"):
        try:
            comfyui.install()
        except comfyui.InstallError as exc:
            say(False, f"ComfyUI: {exc}")
            return 1
        say(True, f"ComfyUI: {config.section('image')['comfy_dir']}")
        return 0
    comfy = ask_path("your own ComfyUI's folder (the one with main.py)", lambda p: (p / "main.py").is_file())
    if not comfy:
        return 1
    python = comfy_python_near(comfy)
    if not (python and yes(f"use ComfyUI's Python at {python}?")):
        python = ask_path("ComfyUI's Python (python.exe on Windows)", Path.is_file)
    if not python:
        return 1
    config.update({"image": {"comfy_dir": str(comfy), "comfy_python": str(python)}}, local=True)
    say(True, f"ComfyUI: {comfy}")
    return 0


def check_image_files(config) -> int:
    """The model files in ComfyUI's model folders (a folder extra_model_paths.yaml maps can't be checked)."""
    from .comfyui import models_dir, needs_gguf

    image = config.section("image")
    comfy = config.path(image.get("comfy_dir"))
    if not (comfy and (comfy / "main.py").is_file()):
        say(None, "the image model files: can't look without the ComfyUI folder")
        return 0
    base = config.path(image.get("comfy_base")) or comfy  # models and custom nodes live here
    models = models_dir(image)
    missing = 0
    if needs_gguf(image):
        nodes = base / "custom_nodes" / "ComfyUI-GGUF"
        say(nodes.is_dir(), "ComfyUI-GGUF custom nodes" if nodes.is_dir() else
            "ComfyUI-GGUF custom nodes (for the .gguf files): `baitbook comfyui` adds them to baitbook's ComfyUI; "
            "for your own, https://github.com/city96/ComfyUI-GGUF into its custom_nodes")
        missing += not nodes.is_dir()
    mapped = (comfy / "extra_model_paths.yaml").is_file()
    for key, folders in FOLDERS.items():
        name = image[key]
        found = next((p for f in folders for p in (models / f).rglob(name) if p.is_file()), None)
        if found:
            say(True, f"[image] {key}: {found}")
        elif mapped:
            say(None, f"[image] {key}: {name} isn't in {models} (fine if extra_model_paths.yaml maps its folder)")
        else:
            say(False, f"[image] {key}: {name} isn't in {models / folders[0]}." + (f" `baitbook models` downloads it: {GET[name]}" if name in GET else ""))
            missing += 1
    return missing


def offer_shortcut() -> None:
    try:
        from . import shortcut
    except ImportError:
        return
    if yes("add baitbook to your Start menu / Applications / app menu?"):
        try:
            for p in shortcut.create():
                say(True, f"app icon: {p}")
        except (RuntimeError, OSError) as exc:
            say(False, f"app icon: {exc}")


def run(check: bool = False) -> int:
    from angler import config

    config.load()
    ask = not check and sys.stdin is not None and sys.stdin.isatty()
    print(f"baitbook setup\n  config  {config.CONFIG_PATH}\n  data    {config.DATA}\n")
    missing = check_llama() + check_comfy(config, ask)  # ComfyUI first: the image models go into it
    if ask:  # the defaults that aren't there yet, from Hugging Face (offer(False) would download unasked)
        from . import models

        models.offer(ask)
    missing += check_text(config, ask) + check_image_files(config)
    if ask:
        offer_shortcut()
    if missing:
        print(f"\n{missing} thing{'s' if missing > 1 else ''} still missing. Run `baitbook setup` again once "
              f"{'they are' if missing > 1 else 'it is'} in place; the rest of the settings are in {config.CONFIG_PATH}.")
        return 1
    print("\nAll set. `baitbook` opens the app.")
    return 0
