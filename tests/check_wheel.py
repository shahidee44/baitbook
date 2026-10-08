"""Check a built wheel, installed in a fresh venv (CI runs this after `uv build`; pytest doesn't collect it).

    <fresh venv>/python -I tests/check_wheel.py dist/baitbook-*.whl

1. The wheel carries the apps' web files, the fonts, the example config and the ComfyUI workflow.
2. Installed, `baitbook --version` / `--help` work and both launchers exist.
3. Installed, the package finds its own web files and example config, and an Angler
   started with `baitbook angler` serves /device/, answers `baitbook status`, and
   stops on `baitbook stop`. Everything lives in a throwaway BAITBOOK_HOME on a free
   loopback port. No model, no ComfyUI.

-I keeps this checkout off sys.path, so what's tested is what the wheel installed.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

NEEDED = ["angler/web/device/index.html", "angler/web/device/sw.js", "angler/web/app/index.html", "angler/web/shared/api.js",
          "angler/web/shared/fonts/fonts.css", "angler/baitbook.example.toml", "angler/workflows/klein_i2i.json",
          "angler/server.py", "baitbook/cli.py", "pcapp/__main__.py"]


def check_contents(wheel: Path) -> None:
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [n for n in NEEDED if n not in names]
    fonts = sorted(n for n in names if re.fullmatch(r"angler/web/shared/fonts/[^/]+\.woff2", n))
    stray = sorted(n for n in names if n.split("/")[0] in ("tests", "data", "docs") or n.rsplit("/", 1)[-1] in ("baitbook.toml", "angler.lock"))
    assert not missing, f"missing from the wheel: {missing}"
    assert len(fonts) >= 8, f"expected the .woff2 fonts in the wheel, found {fonts}"
    assert not stray, f"shouldn't be in the wheel: {stray}"
    print(f"wheel ok: {wheel.name}, {len(names)} files, {len(fonts)} fonts")


def exe(name: str) -> str:
    path = Path(sys.executable).parent / (name + (".exe" if os.name == "nt" else ""))
    assert path.is_file(), f"{path.name} wasn't installed next to {sys.executable}"
    return str(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_installed() -> None:
    home = Path(tempfile.mkdtemp(prefix="baitbook-wheel-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("BAITBOOK_")}
    env.update(BAITBOOK_HOME=str(home), BAITBOOK_SHORTCUT_DIR=str(home / "shortcuts"))
    os.environ.clear()
    os.environ.update(env)  # this process too, before it imports angler below

    def baitbook(*args: str, ok: int = 0) -> str:
        r = subprocess.run([exe("baitbook"), *args], env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == ok, f"`baitbook {' '.join(args)}` exited {r.returncode}, not {ok}:\n{r.stdout}\n{r.stderr}"
        return r.stdout

    assert baitbook("--version").strip().startswith("baitbook ")
    assert "angler" in baitbook("--help")
    exe("baitbook-app")
    print("launchers ok")

    import angler
    from angler import config

    pkg = Path(angler.__file__).resolve().parent
    assert Path(sys.prefix).resolve() in pkg.parents, f"angler imported from {pkg}, not the venv"
    assert config.WEB == pkg / "web" and (config.WEB / "device" / "index.html").is_file(), config.WEB
    assert config.EXAMPLE_PATH == pkg / "baitbook.example.toml" and config.EXAMPLE_PATH.is_file(), config.EXAMPLE_PATH

    # the first load copies the packaged example config; it goes on a free port before anything asks
    # for an Angler, so an Angler already running here on the default port is never seen or stopped
    config.load()
    cfg = home / "baitbook.toml"
    assert cfg.read_text(encoding="utf-8") == config.EXAMPLE_PATH.read_text(encoding="utf-8")
    port = free_port()
    cfg.write_text(re.sub(r"(?m)^port = \d+", f"port = {port}", cfg.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    assert "not running" in baitbook("status", ok=1)
    with open(home / "angler.log", "wb") as log:
        proc = subprocess.Popen([exe("baitbook"), "angler", "--host", "127.0.0.1"], env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/device/", timeout=2) as r:
                        assert r.status == 200 and b"<!doctype html>" in r.read().lower()
                        break
                except OSError:
                    assert proc.poll() is None and time.monotonic() < deadline, (home / "angler.log").read_text(errors="replace")
                    time.sleep(0.2)
            assert "Pairing code" in baitbook("status")
            assert "The Angler has stopped." in baitbook("stop")
            assert proc.wait(timeout=30) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
    shutil.rmtree(home, ignore_errors=True)
    print("installed Angler ok: served /device/, answered `baitbook status`, stopped on `baitbook stop`")


if __name__ == "__main__":
    wheels = [Path(p) for p in sys.argv[1:]]
    assert len(wheels) == 1 and wheels[0].suffix == ".whl", f"give exactly one wheel, not {sys.argv[1:]}"
    check_contents(wheels[0])
    check_installed()
