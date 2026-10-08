"""The language + vision model, in-process through llama-cpp-python.

Gemma 4 31B (Google's QAT Q4_0 GGUF) with its mmproj by default; any GGUF + projector the
Gemma 4 chat handler can drive is drop-in via [text] in baitbook.toml. One
model, loaded lazily, used by one caller at a time.
"""

from __future__ import annotations

import base64
import gc
import logging
import threading
import time

from . import config

log = logging.getLogger("angler.llm")


class LLMError(RuntimeError):
    pass


def image_part(png: bytes, mime: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(png).decode()}"}}


class LanguageModel:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg  # the [text] section of baitbook.toml
        self._llm = None
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._llm is not None

    def load(self) -> None:
        with self._lock:
            if self._llm is not None:
                return
            try:
                from llama_cpp import Llama
                from llama_cpp.llama_chat_format import Gemma4ChatHandler
            except ImportError as exc:
                raise LLMError("llama-cpp-python is not installed in this environment.") from exc
            c = self.cfg
            model, mmproj = config.path(c.get("model")), config.path(c.get("mmproj"))
            for key, p in (("model", model), ("mmproj", mmproj)):
                if (key == "model" or p) and not (p and p.is_file()):
                    raise LLMError(f"[text] {key} is not a file: {c.get(key)!r}. Set it with `baitbook setup`, in "
                                   "baitbook.toml or in the PC app's Settings.")
            t = time.time()
            handler = Gemma4ChatHandler(clip_model_path=str(mmproj), verbose=False) if mmproj else None
            # Tested settings for Gemma 4 (up to 31B): a Gemma 4
            # image is decoded in one micro-batch of image_max_tokens (560) + 16.
            batch = int(c.get("n_batch", 576))
            # GPU memory freed by ComfyUI (or a previous load) comes back a moment
            # late, so a load right after a swap can fail once or twice.
            for attempt in range(4):
                try:
                    self._llm = self._make(Llama, str(model), handler, batch)
                    break
                except ValueError:
                    if attempt == 3:
                        raise LLMError("The language model could not be loaded; is the GPU memory in use?") from None
                    log.warning("language model load failed, retrying in 8s")
                    gc.collect()
                    time.sleep(8)
            log.info("language model loaded in %.1fs", time.time() - t)

    def _make(self, Llama, model_path, handler, batch):  # noqa: N803
        c = self.cfg
        return Llama(
                model_path=model_path,
                chat_handler=handler,
                n_ctx=int(c.get("n_ctx", 16384)),
                n_gpu_layers=int(c.get("n_gpu_layers", -1)),
                n_batch=batch,
                n_ubatch=batch,
                # Only keep the sliding-window layers' window, not the whole context:
                # 3.5 GB of KV at 16k instead of 12.8 GB, which leaves room for klein.
                swa_full=bool(c.get("swa_full", False)),
                use_mmap=bool(c.get("use_mmap", True)),
                verbose=False,
            )

    def unload(self) -> None:
        with self._lock:
            if self._llm is None:
                return
            try:
                self._llm.close()
            except Exception:  # noqa: BLE001
                pass
            self._llm = None
            gc.collect()
            log.info("language model unloaded")

    def chat(self, messages: list[dict], *, max_tokens: int = 400, temperature: float | None = None) -> str:
        with self._lock:
            self.load()
            c = self.cfg
            out = self._llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=float(c.get("temperature", 1.0) if temperature is None else temperature),
                top_p=float(c.get("top_p", 0.95)),
                top_k=int(c.get("top_k", 64)),
                min_p=float(c.get("min_p", 0.05)),
                repeat_penalty=float(c.get("repeat_penalty", 1.1)),
            )
            return out["choices"][0]["message"]["content"] or ""
