"""Shared setup. Nothing in the suite needs a model, a GPU, ComfyUI or the internet.

The Angler reads where its config and data live once, at import (angler/config.py),
so BAITBOOK_HOME / BAITBOOK_CONFIG / BAITBOOK_DATA are pointed at a throwaway
folder here, before anything imports angler. On top of that, every test gets its
own empty home (the autouse `home` fixture): config, database and images in
tmp_path, with the config and database caches reset. The owner's real config
and data (%LOCALAPPDATA%\\baitbook, the repo's data/ and baitbook.toml) and app
menus are never touched, and the suite refuses to connect to 8787 / 8788 (a
running Angler) or 8188 (ComfyUI).
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # always test this checkout

SESSION_HOME = Path(tempfile.mkdtemp(prefix="baitbook-tests-"))
os.environ["BAITBOOK_HOME"] = str(SESSION_HOME)
os.environ["BAITBOOK_CONFIG"] = str(SESSION_HOME / "baitbook.toml")
os.environ["BAITBOOK_DATA"] = str(SESSION_HOME / "data")
os.environ["BAITBOOK_SHORTCUT_DIR"] = str(SESSION_HOME / "shortcuts")  # never the real Start menu / Applications / app menu
assert "angler" not in sys.modules, "angler was imported before the test environment was set"

# ---------- never talk to the owner's Angler or ComfyUI ----------
FORBIDDEN_PORTS = {8787, 8788, 8188}
_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex


def _check(address) -> None:
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in FORBIDDEN_PORTS:
        raise ConnectionRefusedError(f"the tests never connect to port {address[1]} (a real Angler or ComfyUI)")


def _guarded_connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

from angler import config, db, engine as engine_mod, imagegen, media, tailscale  # noqa: E402

assert Path(config.CONFIG_PATH).is_relative_to(SESSION_HOME) and Path(config.DATA).is_relative_to(SESSION_HOME)

from tests.support import FakeComfy, FakeLM, wait_idle  # noqa: E402

REAL_ALIVE = imagegen.ComfyClient.alive
REAL_ENSURE_RUNNING = imagegen.ComfyClient.ensure_running
REAL_TAILSCALE_EXE = tailscale.exe


def pytest_configure(config):  # pytest's name for the argument; it shadows angler.config only in here
    config.addinivalue_line("markers", "smoke: starts a real Angler process on a free port")
    # third-party deprecations the suite can't act on (FastAPI's on_event is reported separately)
    config.addinivalue_line("filterwarnings", r"ignore:\s*on_event is deprecated:DeprecationWarning")
    config.addinivalue_line("filterwarnings", "ignore:Using `httpx` with `starlette.testclient` is deprecated")


def pytest_sessionfinish(session, exitstatus):
    _close_db()
    shutil.rmtree(SESSION_HOME, ignore_errors=True)


def _close_db() -> None:
    if db._conn is not None:
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None


# ---------- per test ----------
@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    """A fresh, empty baitbook home for this test. The config file isn't there until
    something loads the config (which copies baitbook.example.toml, as on a first run)."""
    h = tmp_path / "home"
    h.mkdir()
    data = h / "data"
    monkeypatch.setattr(config, "HOME", h)
    monkeypatch.setattr(config, "CONFIG_PATH", h / "baitbook.toml")
    monkeypatch.setattr(config, "DATA", data)
    monkeypatch.setattr(config, "IMAGES", data / "images")
    monkeypatch.setattr(config, "DB_PATH", data / "baitbook.db")
    monkeypatch.setattr(config, "_cfg", {})
    monkeypatch.setattr(config, "_overrides", {})  # `angler --port` and the like
    monkeypatch.setenv("BAITBOOK_SHORTCUT_DIR", str(tmp_path / "shortcuts"))
    _close_db()
    media.avg_color.cache_clear()
    yield h
    _close_db()
    media.avg_color.cache_clear()


@pytest.fixture(autouse=True)
def no_outside_world(monkeypatch):
    """Safety nets: ComfyUI is never reachable or started, the scheduler thread never
    ticks, no job keeps the machine awake, and the owner's Tailscale is never asked or
    changed (as if it weren't installed). Tests that need the real ComfyClient against a
    fake server put REAL_ALIVE / REAL_ENSURE_RUNNING back themselves."""
    monkeypatch.setattr(tailscale, "exe", lambda: None)
    monkeypatch.setattr(imagegen.ComfyClient, "alive", lambda self: False)

    def _no_comfy(self, wait_s: float = 180) -> None:
        raise imagegen.ImageGenError("the tests never start ComfyUI")

    monkeypatch.setattr(imagegen.ComfyClient, "ensure_running", _no_comfy)
    monkeypatch.setattr(imagegen, "_PROC", None)
    monkeypatch.setattr(engine_mod, "keep_awake", lambda on: None)
    monkeypatch.setattr(engine_mod.Engine, "_scheduler", lambda self: None)


@pytest.fixture
def fakes(monkeypatch):
    """A fake language model and a fake ComfyClient behind every Engine."""
    lm, comfy = FakeLM(), FakeComfy()
    monkeypatch.setattr(engine_mod.Engine, "lm", lambda self: lm)
    monkeypatch.setattr(engine_mod.Engine, "comfy", lambda self: comfy)
    return lm, comfy


@pytest.fixture
def make_engine(fakes):
    """Engines built in this test; each is left idle before the next test starts."""
    made = []

    def make():
        config.load()
        e = engine_mod.Engine()
        made.append(e)
        return e

    yield make
    for e in made:
        wait_idle(e)


@pytest.fixture
def engine(make_engine):
    return make_engine()
