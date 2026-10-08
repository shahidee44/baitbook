"""Test helpers: tiny images, data seeding, and fakes for the two models.

Nothing here loads a model, starts ComfyUI or talks to the network. Every
function writes through angler.config / angler.db, which the `home` fixture
(conftest.py) points at a fresh temporary folder for each test.
"""

from __future__ import annotations

import io
import itertools
import re
import socket
import time

from PIL import Image

from angler import config, db, media
from angler import prompts as P

ROOT = config.ROOT


# ---------- images ----------
def png(color: tuple[int, int, int] = (200, 80, 40), size: tuple[int, int] = (16, 16), mode: str = "RGB") -> bytes:
    """A tiny solid PNG, made in memory."""
    im = Image.new(mode, size, color if mode == "RGB" else (*color, 128))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def free_port() -> int:
    """A port nothing is listening on right now (never 8787 / 8788)."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in (8787, 8788, 8188):
            return port


def write_config(home, port: int) -> None:
    """baitbook.toml for an Angler a test talks to: the example config, but on a free
    loopback port, with the schedule off and ComfyUI's address somewhere nothing listens."""
    text = config.EXAMPLE_PATH.read_text(encoding="utf-8")
    for old, new in ((r"^host = .*$", 'host = "127.0.0.1"'), (r"^port = 8787\b", f"port = {port}"),
                     (r"^schedule_enabled = true", "schedule_enabled = false"),
                     (r'^comfy_url = .*$', f'comfy_url = "http://127.0.0.1:{free_port()}"')):
        text, n = re.subn(old, new, text, count=1, flags=re.M)
        assert n == 1, f"the example config has no line matching {old!r}"
    (home / "baitbook.toml").write_text(text, encoding="utf-8")


# ---------- data ----------
_colors = itertools.cycle([(200, 80, 40), (40, 120, 200), (90, 160, 70), (220, 200, 60), (120, 60, 160)])


def add_resident(rid: str = "moss", status: str = "ready", n_seeds: int = 2, moods: tuple[str, ...] = ("hushed", "sun warmed"),
                 with_images: bool = True) -> dict:
    """A resident with `n_seeds` seed posts (each with an image and a base prompt)."""
    config.get()  # loaded: the images folder exists
    db.add_resident(rid, rid.title(), rid, f"{rid} lives by the sea", "#E8C9A8")
    db.update_resident(rid, status=status, moods=list(moods))
    for i in range(n_seeds):
        stem = media.save(png(next(_colors))) if with_images else None
        db.add_post(resident_id=rid, kind="seed", image=stem, prompt=f"seed photograph {i} of {rid} standing on a quiet pier at dawn",
                    shift_id=0)
    return db.resident(rid)


def add_edit(rid: str, source_id: int, vote: int = 0, snagged: bool = False, created_at: float | None = None,
             with_image: bool = True, prompt: str | None = None, shift_id: int = 1) -> int:
    """An edit post of `source_id`, optionally voted / snagged / backdated."""
    config.get()
    stem = media.save(png(next(_colors))) if with_image else None
    fields = dict(resident_id=rid, kind="edit", source_id=source_id, prompt=prompt or f"edit of {source_id} {time.perf_counter_ns()}",
                  caption="a caption", tags=["hushed"], image=stem, seed=7, shift_id=shift_id)
    if created_at is not None:
        fields["created_at"] = created_at
    pid = db.add_post(**fields)
    if vote:
        db.vote(pid, vote, "pc")
    if snagged:
        db.snag(pid)
    return pid


# ---------- the engine ----------
def worker_waiting(engine) -> bool:
    """The engine's worker thread is blocked on an empty queue: every job it took is
    completely over, its journal entry and clean-up included."""
    cond = engine.jobs.not_empty
    return engine.jobs.empty() and len(getattr(cond, "_waiters", ())) > 0


def wait_idle(engine, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker_waiting(engine):
            return
        time.sleep(0.005)
    raise AssertionError(f"the engine is still busy after {timeout}s: {engine.status}")


def wait_for(predicate, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class FakeLM:
    """Stands in for llm.LanguageModel. Answers each kind of ask the engine makes with
    text that never resembles anything else (so no edit is sent back as too close),
    unless a test passes its own `answer(messages) -> str`."""

    def __init__(self, answer=None) -> None:
        self.answer = answer or self.default_answer
        self.calls: list[list[dict]] = []
        self.loads = self.unloads = 0
        self._n = itertools.count(1)

    def load(self) -> None:
        self.loads += 1

    def unload(self) -> None:
        self.unloads += 1

    def chat(self, messages: list[dict], *, max_tokens: int = 400, temperature: float | None = None) -> str:
        self.calls.append([dict(m) for m in messages])
        return self.answer(messages)

    def default_answer(self, messages: list[dict]) -> str:
        k = next(self._n)
        last = messages[-1]["content"]
        if isinstance(last, list):  # a vision read turn: [image, text]
            return f'Prompt: "a weathered lighthouse number{k} under violet{k} storm clouds, film grain"'
        if last == P.MOOD_ASK:
            return "Hushed, Sun-warmed , domestic,,"
        if last == P.CAPTION_ASK:
            return f"Caption: Out by the water again {k}\nsecond line is dropped"
        return f"zebra{k} quartz{k} orchard{k} lantern{k} meadow{k} copper{k} harbour{k} tram{k}"


class FakeComfy:
    """Stands in for imagegen.ComfyClient: every edit comes back as a small PNG."""

    def __init__(self, fail_prompts: tuple[str, ...] = ()) -> None:
        self.fail_prompts = fail_prompts
        self.edits: list[tuple[bytes, str, int]] = []
        self.ensured = self.released = 0
        self.cfg = {}

    def ensure_running(self) -> None:
        self.ensured += 1

    def release(self) -> None:
        self.released += 1

    def edit(self, source_png: bytes, prompt: str, seed: int) -> bytes:
        self.edits.append((source_png, prompt, seed))
        if any(f in prompt for f in self.fail_prompts):
            from angler.imagegen import ImageGenError
            raise ImageGenError("ComfyUI run failed (fake)")
        return png((10, 200, 120), (24, 24))
