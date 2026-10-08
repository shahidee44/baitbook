"""Image files: a PNG master (what the image model edits) plus JPEG variants
for clients — full size for the feed, a small thumbnail for grids and the reef."""

from __future__ import annotations

import functools
import io
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from . import config

FULL_JPEG = 1024
THUMB = 320


def _variants(stem: str, im: Image.Image) -> None:
    rgb = im.convert("RGB")
    full = rgb.copy()
    full.thumbnail((FULL_JPEG, FULL_JPEG))
    full.save(config.IMAGES / f"{stem}.jpg", "JPEG", quality=86, optimize=True)
    thumb = ImageOps.fit(rgb, (THUMB, THUMB), Image.LANCZOS)
    thumb.save(config.IMAGES / f"{stem}_t.jpg", "JPEG", quality=82, optimize=True)


class NotAnImage(ValueError):
    """Bytes that can't be read as an image."""


def save(data: bytes) -> str:
    """Store any image the owner uploads or the image model returns. Returns its stem.
    NotAnImage if `data` isn't one; then, as on any failure, no file is left behind."""
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception as exc:  # noqa: BLE001  (a broken or foreign file can raise OSError, SyntaxError, ValueError, ...)
        raise NotAnImage(str(exc)) from exc
    stem = uuid.uuid4().hex[:16]
    try:
        im.save(config.IMAGES / f"{stem}.png", "PNG")
        _variants(stem, im)
    except BaseException:
        delete(stem)
        raise
    return stem


def png(stem: str) -> bytes:
    return (config.IMAGES / f"{stem}.png").read_bytes()


def path(name: str) -> Path | None:
    """A servable file (stem.jpg / stem_t.jpg / stem.png), or None."""
    p = (config.IMAGES / name).resolve()
    return p if p.parent == config.IMAGES.resolve() and p.is_file() else None


def delete(stem: str) -> None:
    for suffix in (".png", ".jpg", "_t.jpg"):
        (config.IMAGES / f"{stem}{suffix}").unlink(missing_ok=True)


@functools.lru_cache(maxsize=4096)
def avg_color(stem: str) -> str:
    """The image's average colour (for reef branches)."""
    try:
        with Image.open(config.IMAGES / f"{stem}_t.jpg") as im:
            r, g, b = im.convert("RGB").resize((1, 1), Image.LANCZOS).getpixel((0, 0))
        return f"#{r:02x}{g:02x}{b:02x}"
    except OSError:
        return "#c9a27a"
