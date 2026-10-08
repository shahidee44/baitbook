"""baitbook.toml: read on start, written back when the PC app changes a setting.

A user's config and data live in one folder of their own (HOME), outside the
install, so upgrading or reinstalling never touches them:
  Windows  %LOCALAPPDATA%\\baitbook      macOS  ~/Library/Application Support/baitbook
  Linux    $XDG_DATA_HOME/baitbook (~/.local/share/baitbook)
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import shutil
import sys
import threading
import tomllib
from pathlib import Path

import json
import re


def _home() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "baitbook"


PKG = Path(__file__).resolve().parent
ROOT = PKG.parent  # a source checkout; installed, the files below travel inside the package
WEB = PKG / "web" if (PKG / "web").is_dir() else ROOT / "web"
EXAMPLE_PATH = next((p for p in (PKG / "baitbook.example.toml", ROOT / "baitbook.example.toml") if p.is_file()), ROOT / "baitbook.example.toml")
# BAITBOOK_HOME moves the whole folder; BAITBOOK_CONFIG / BAITBOOK_DATA point a second Angler (a test, say) elsewhere
HOME = Path(os.environ.get("BAITBOOK_HOME") or _home())
CONFIG_PATH = Path(os.environ.get("BAITBOOK_CONFIG") or HOME / "baitbook.toml")
DATA = Path(os.environ.get("BAITBOOK_DATA") or HOME / "data")
IMAGES = DATA / "images"
DB_PATH = DATA / "baitbook.db"


def code_version() -> str:
    """A hash of the Angler's Python (angler/) as it is on disk now. The Angler takes it as it
    starts, so an Angler older than its code (a source checkout changed, an upgrade installed
    since) can be told apart. web/ isn't in it: the Angler reads those files fresh every time."""
    h = hashlib.sha256()
    for path in sorted(PKG.rglob("*.py")):
        h.update(path.relative_to(PKG).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()[:12]

# What the PC app may edit, and the type each value must have.
EDITABLE = {
    "text": {"name": str, "model": str, "mmproj": str, "n_ctx": int, "temperature": float},
    "image": {"name": str, "unet": str, "text_encoder": str, "vae": str, "width": int, "height": int, "steps": int, "cfg": float, "primer_file": str},
    "shift": {"posts_per_resident": int, "good_examples": int, "bad_examples": int, "variation": str,
              "schedule_enabled": bool, "schedule_time": str, "catch_up_hours": float},
    "device": {"keep_nights": int},
}
# Settings only this machine may write (`baitbook setup`, `baitbook comfyui`): they say which
# program the Angler runs and which ComfyUI it talks to. PUT /api/config refuses them.
LOCAL_ONLY = {"image": {"comfy_dir": str, "comfy_python": str, "comfy_base": str, "comfy_url": str}}
VARIATIONS = ("subtle", "bold", "wild")
# Numbers refused outside these bounds (inclusive): a value past them would stop the schedule, the shift or the models.
LIMITS = {("text", "n_ctx"): (2048, 1_048_576), ("text", "temperature"): (0.0, 2.0),
          ("image", "width"): (64, 4096), ("image", "height"): (64, 4096), ("image", "steps"): (1, 200), ("image", "cfg"): (0.0, 100.0),
          ("shift", "posts_per_resident"): (1, 100), ("device", "keep_nights"): (1, 365)}
# Numbers moved into these bounds instead.
CLAMPS = {("shift", "catch_up_hours"): (0.0, 24.0), ("shift", "good_examples"): (0, 10), ("shift", "bad_examples"): (0, 10)}

_lock = threading.Lock()
_cfg: dict = {}
_overrides: dict = {}  # {section: {key: value}} for this process only: see override()


def load() -> dict:
    global _cfg
    with _lock:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        # the example's values fill in whatever an older or hand-written file leaves out (in memory only)
        _cfg = _defaults()
        for sec, kv in tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")).items():
            if isinstance(kv, dict):
                _cfg.setdefault(sec, {}).update(kv)
            else:
                _cfg[sec] = kv
        for sec, kv in _overrides.items():
            _cfg.setdefault(sec, {}).update(kv)
        IMAGES.mkdir(parents=True, exist_ok=True)
        return copy.deepcopy(_cfg)


def override(changes: dict) -> dict:
    """Values for this process only, such as `baitbook angler --port`: never written to the file,
    and laid over it again by every later load(), so whatever reads the config sees them."""
    with _lock:
        for sec, kv in changes.items():
            _overrides.setdefault(sec, {}).update(kv)
    return load()


def _defaults() -> dict:
    try:
        return tomllib.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


def get() -> dict:
    if not _cfg:
        load()
    return copy.deepcopy(_cfg)


def section(name: str) -> dict:
    return get()[name]


def path(value: str | None) -> Path | None:
    """A path from the config: absolute, or relative to the config file. None if empty."""
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else CONFIG_PATH.parent / p


def local_port(server: dict) -> int:
    """Where the PC app on this machine reaches the Angler, over plain HTTP on 127.0.0.1:
    [server] port, or with HTTPS on, the loopback-only [server] local_port (no cert names 127.0.0.1)."""
    if server.get("ssl_certfile"):
        return int(server.get("local_port") or 8788)
    return int(server["port"])


def angler_port(server: dict) -> int:
    """local_port(), unless the Angler running on this data folder noted another in data/angler.port:
    one started with `baitbook angler --port` listens there, whatever the file says. A note left by
    an Angler that was killed is never believed: the data folder's lock says whether one runs."""
    port = local_port(server)
    try:
        noted = int((DATA / "angler.port").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return port
    if noted == port:
        return port
    from .__main__ import data_in_use  # here, not at the top: __main__ imports this module (and uvicorn)

    return noted if data_in_use() else port


def update(changes: dict, local: bool = False) -> dict:
    """Apply {section: {key: value}} from the PC app (or, local=True, `baitbook setup`),
    type-checked, and save."""
    get()  # loaded
    with _lock:
        if not all(isinstance(kv, dict) for kv in changes.values()):
            raise ValueError("give settings as {section: {key: value}}")
        checked = [(sec, key, _check(sec, key, value, local)) for sec, kv in changes.items() for key, value in kv.items()]
        for sec, key, value in checked:  # only once every value has passed: all or nothing
            _cfg.setdefault(sec, {})[key] = value
            _write_value(sec, key, value)
        return copy.deepcopy(_cfg)


def _check(sec: str, key: str, value, local: bool):
    """One setting, converted to its type and kept in range; ValueError if it can't be."""
    allowed = {**EDITABLE.get(sec, {}), **(LOCAL_ONLY.get(sec, {}) if local else {})}
    if not allowed:
        raise ValueError(f"unknown section {sec!r}")
    if key not in allowed:
        raise ValueError(f"{sec}.{key} can't be changed from the app")
    typ = allowed[key]
    try:
        if typ is bool:
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
        else:
            value = typ(value)
    except (TypeError, OverflowError) as exc:  # a list, null, or an infinite whole number
        raise ValueError(f"{sec}.{key} can't be {value!r}") from exc
    if typ is float and not math.isfinite(value):
        raise ValueError(f"{sec}.{key} must be a finite number")
    if sec == "shift" and key == "schedule_time":
        hh, mm = (int(x) for x in value.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("shift.schedule_time must be a time of day, 00:00 to 23:59")
        value = f"{hh:02d}:{mm:02d}"
    if sec == "shift" and key == "variation" and value not in VARIATIONS:
        raise ValueError(f"shift.variation must be one of {', '.join(VARIATIONS)}")
    if (sec, key) in CLAMPS:
        lo, hi = CLAMPS[sec, key]
        value = max(lo, min(hi, value))
    if (sec, key) in LIMITS:
        lo, hi = LIMITS[sec, key]
        if not lo <= value <= hi:
            raise ValueError(f"{sec}.{key} must be from {lo} to {hi}")
    return value


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string


# `key = value   # comment` where value is a "string" (which may contain #) or a bare token
_LINE = re.compile(r'^(?P<lead>\s*(?P<key>[A-Za-z0-9_]+)\s*=\s*)(?P<val>"(?:[^"\\]|\\.)*"|[^#]*?)(?P<gap>\s*)(?P<comment>#.*)?$')


def _write_value(sec: str, key: str, value) -> None:
    """Rewrite one `key = value` line in place, keeping the file's comments."""
    new = _toml_value(value)
    lines = CONFIG_PATH.read_text(encoding="utf-8-sig").splitlines()
    header = re.compile(rf"^\s*\[\s*{re.escape(sec)}\s*\]\s*(#.*)?$")  # `[shift]`, maybe with a comment
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        lines += ["", f"[{sec}]", f"{key} = {new}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        for i in range(start + 1, end):
            m = _LINE.match(lines[i])
            if m and m.group("key") == key:
                comment = m.group("comment")
                if comment:
                    width = len(m.group("val")) + len(m.group("gap"))  # keep the comment column
                    lines[i] = m.group("lead") + new.ljust(max(width - 1, len(new))) + " " + comment
                else:
                    lines[i] = m.group("lead") + new
                break
        else:
            lines.insert(start + 1, f"{key} = {new}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
