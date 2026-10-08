"""The image model: FLUX.2 [klein] (4B or 9B) image-to-image edits, run through ComfyUI.

Every baitbook image after the seeds is an edit of an older one: the source
image goes in as a reference latent, the edit prompt describes the whole target
image. The graph lives in workflows/klein_i2i.json; this module fills in the
files, prompt, size, steps, cfg and seed from the config, queues it on a
ComfyUI server (starting one if asked to), and hands back the PNG bytes.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("angler.image")

WORKFLOW = Path(__file__).parent / "workflows" / "klein_i2i.json"

# The ComfyUI process the Angler started itself (None if it found one already running).
_PROC: subprocess.Popen | None = None


def stop_started_comfy() -> bool:
    """Stop the ComfyUI the Angler started, if it's running. True if there was one."""
    global _PROC
    if _PROC is None or _PROC.poll() is not None:
        return False
    log.info("stopping the ComfyUI the Angler started, to free the GPU")
    _PROC.terminate()
    try:
        _PROC.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _PROC.kill()
    _PROC = None
    return True


class ImageGenError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg  # the [image] section of baitbook.toml
        self.url = cfg["comfy_url"].rstrip("/")

    # ---------- server ----------
    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/system_stats", timeout=3) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def ours(self) -> bool:
        """The ComfyUI answering at comfy_url is the one this config names. With [image]
        comfy_base set (baitbook's own ComfyUI) it must have been started with that
        --base-directory; any other ComfyUI on the port (yours, on its default port, say)
        has none of baitbook's models or nodes. Without comfy_base, any ComfyUI will do."""
        base = config.path(self.cfg.get("comfy_base"))
        if not base:
            return True
        try:
            with urllib.request.urlopen(self.url + "/system_stats", timeout=3) as r:
                argv = json.load(r).get("system", {}).get("argv", [])
        except (urllib.error.URLError, OSError, ValueError):
            return False
        given = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--base-directory"), None)
        return bool(given) and Path(given).resolve() == base.resolve()

    def check_ours(self) -> None:
        if not self.ours():
            raise ImageGenError(f"Another ComfyUI is running at {self.url}, not baitbook's own. Stop it, or move baitbook's "
                                "to a free port with [image] comfy_url (`baitbook comfyui` picks one).")

    def ensure_running(self, wait_s: float = 180) -> None:
        """Start ComfyUI from the configured folder if nothing answers at comfy_url."""
        if self.alive():
            self.check_ours()
            return
        if not self.cfg.get("comfy_autostart", True):
            raise ImageGenError(f"ComfyUI is not reachable at {self.url} and autostart is off.")
        comfy_dir = config.path(self.cfg.get("comfy_dir")) or Path("")
        python = config.path(self.cfg.get("comfy_python")) or Path("")
        if not (comfy_dir / "main.py").is_file() or not python.is_file():
            raise ImageGenError(f"ComfyUI is not reachable at {self.url}, and [image] comfy_dir / comfy_python don't point at a "
                                "ComfyUI folder and its python.exe, so it can't be started. Set them in baitbook.toml.")
        port = self.url.rsplit(":", 1)[-1]
        global _PROC
        log.info("starting ComfyUI from %s", comfy_dir)
        logf = open(Path(self.cfg.get("comfy_log", "comfy.log")), "ab")
        base = config.path(self.cfg.get("comfy_base"))  # models, custom nodes and outputs kept outside the code
        _PROC = subprocess.Popen(
            [str(python), "main.py", "--listen", "127.0.0.1", "--port", port, "--disable-auto-launch",
             *(["--base-directory", str(base)] if base else [])],
            cwd=comfy_dir, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.alive():
                return
            if _PROC.poll() is not None:
                raise ImageGenError("ComfyUI exited while starting; see its log.")
            time.sleep(2)
        raise ImageGenError("ComfyUI did not come up in time.")

    def release(self) -> None:
        """Give the GPU back before the language model loads.

        A ComfyUI the Angler started is simply stopped: its own unload-all
        (/free) can crash the ROCm build on Windows, and a process exit always
        returns the memory. A ComfyUI someone else started is only asked to
        free its models, and left running.
        """
        if stop_started_comfy():
            time.sleep(2)
        elif self.alive() and self.ours():  # never someone else's ComfyUI on our port
            try:
                self._post("/free", {"unload_models": True, "free_memory": True})
            except Exception as exc:  # noqa: BLE001
                log.warning("ComfyUI /free failed: %s", exc)
            time.sleep(3)

    # ---------- http ----------
    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise ImageGenError(f"ComfyUI {path} failed: {e.read().decode('utf-8', 'replace')[:800]}") from e

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=60) as r:
            return r.read()

    def upload(self, png: bytes, name: str) -> str:
        """Put a source image in ComfyUI's temp folder, which ComfyUI empties each time it starts,
        so no copy stays behind in its input folder. Returns the name LoadImage takes for it."""
        boundary = uuid.uuid4().hex
        field = lambda k, v: f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}"  # noqa: E731
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode() + png + (field("overwrite", "true") + field("type", "temp") + f"\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            got = json.load(r)
        path = "/".join(p for p in (got.get("subfolder"), got["name"]) if p)
        return f"{path} [{got.get('type') or 'input'}]"  # "[temp]": where LoadImage looks for it

    # ---------- the edit ----------
    def build(self, source_name: str, prompt: str, seed: int) -> dict:
        c = self.cfg
        wf = {k: v for k, v in json.loads(WORKFLOW.read_text(encoding="utf-8")).items() if not k.startswith("_")}
        wf = copy.deepcopy(wf)
        # GGUF files go through ComfyUI-GGUF's loaders; safetensors through ComfyUI's own
        if c["unet"].lower().endswith(".gguf"):
            wf["1"] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": c["unet"]}}
        else:
            wf["1"] = {"class_type": "UNETLoader", "inputs": {"unet_name": c["unet"], "weight_dtype": "default"}}
        clip = "CLIPLoaderGGUF" if c["text_encoder"].lower().endswith(".gguf") else "CLIPLoader"
        wf["2"] = {"class_type": clip, "inputs": {"clip_name": c["text_encoder"], "type": "flux2"}}
        wf["3"]["inputs"]["vae_name"] = c["vae"]
        wf["4"]["inputs"]["text"] = prompt
        wf["20"]["inputs"]["image"] = source_name
        wf["21"]["inputs"]["megapixels"] = float(c.get("reference_megapixels", 1.0))
        wf["6"]["inputs"].update(width=int(c["width"]), height=int(c["height"]))
        wf["9"]["inputs"].update(steps=int(c["steps"]), width=int(c["width"]), height=int(c["height"]))
        wf["10"]["inputs"]["cfg"] = float(c["cfg"])
        wf["7"]["inputs"]["noise_seed"] = int(seed)
        if c.get("tiled_decode", True):
            wf["12"]["inputs"]["tile_size"] = int(c.get("tile_size", 512))
        else:
            wf["12"] = {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}}
        return wf

    def edit(self, source_png: bytes, prompt: str, seed: int, timeout_s: float = 900) -> bytes:
        """Edit `source_png` into the image `prompt` describes. Returns PNG bytes."""
        self.ensure_running()
        name = self.upload(source_png, f"bb_src_{uuid.uuid4().hex[:10]}.png")
        queued = self._post("/prompt", {"prompt": self.build(name, prompt, seed), "client_id": "baitbook"})
        pid = queued["prompt_id"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            hist = json.loads(self._get(f"/history/{pid}") or b"{}")
            if pid in hist:
                entry = hist[pid]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise ImageGenError(f"ComfyUI run failed: {json.dumps(status.get('messages', []))[:800]}")
                for out in entry.get("outputs", {}).values():
                    for img in out.get("images", []):
                        q = f"/view?filename={urllib.parse.quote(img['filename'])}&subfolder={urllib.parse.quote(img.get('subfolder', ''))}&type={img.get('type', 'output')}"
                        return self._get(q)
                if status.get("completed"):
                    raise ImageGenError("ComfyUI finished without an image.")
            time.sleep(1.0)
        raise ImageGenError("Timed out waiting for ComfyUI.")
