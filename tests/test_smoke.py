"""A real Angler process: `python -m angler` on a free loopback port, with a throwaway home.

It starts, serves the API and both apps, refuses to run twice on the same data,
answers `baitbook status`, and stops on `baitbook stop`. No model is involved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from angler import config
from angler.__main__ import data_in_use
from baitbook import cli
from tests.support import ROOT, free_port, write_config

pytestmark = pytest.mark.smoke


def get(url: str, timeout: float = 5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), r.headers  # case-insensitive lookups
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def env_for(home) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BAITBOOK_")}
    env.update(BAITBOOK_HOME=str(home), BAITBOOK_CONFIG=str(home / "baitbook.toml"), BAITBOOK_DATA=str(home / "data"),
               BAITBOOK_SHORTCUT_DIR=str(home / "shortcuts"), PYTHONUNBUFFERED="1",
               PYTHONPATH=os.pathsep.join([str(ROOT), *filter(None, [env.get("PYTHONPATH")])]))
    return env


def start(home, port: int, log_name: str, *args: str) -> tuple[subprocess.Popen, object]:
    log = open(home / log_name, "wb")
    proc = subprocess.Popen([sys.executable, "-m", "angler", "--host", "127.0.0.1", "--port", str(port), *args],
                            cwd=home, env=env_for(home), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return proc, log


def output(home, log_name: str) -> str:
    return (home / log_name).read_text(encoding="utf-8", errors="replace")


def wait_up(proc: subprocess.Popen, base: str, home, log_name: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"the Angler exited with {proc.returncode} while starting:\n{output(home, log_name)}")
        try:
            code, body, _ = get(base + "/api/status", timeout=20)
            if code == 200:
                return json.loads(body)
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    pytest.fail(f"the Angler didn't answer within {timeout}s:\n{output(home, log_name)}")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=30)


def test_angler_process(home, capsys):
    port = free_port()
    write_config(home, port)
    base = f"http://127.0.0.1:{port}"
    first = second = None
    logs = []
    try:
        first, log = start(home, port, "angler.log", "--background")  # as the PC app starts it
        logs.append(log)
        status = wait_up(first, base, home, "angler.log")
        t = time.monotonic()
        get(base + "/api/status")
        took = time.monotonic() - t
        assert took < 2.5, (f"GET /api/status took {took:.1f}s (lan_urls() resolves this machine's name on every call), "
                            "so `baitbook status` / `stop` (3 s timeout) would say no Angler is running")
        assert status["owner"] is True and status["engine"]["state"] == "idle"
        assert status["counts"]["residents"] == 0 and len(status["pair_code"]) == 6
        assert status["code"] == config.code_version() and status["stale"] is False and status["background"] is True

        # the first run made its config and data under the throwaway home, nowhere else
        assert (home / "data" / "baitbook.db").is_file() and (home / "data" / "angler.lock").is_file()
        assert (home / "data" / "angler.port").read_text(encoding="ascii") == str(port)

        for path in ("/app/", "/device/"):
            code, body, headers = get(base + path)
            assert code == 200 and b"<!doctype html>" in body.lower(), path
        code, body, headers = get(base + "/device/sw.js")
        assert code == 200 and b"const VERSION = 'dev';" not in body and headers.get("Cache-Control") == "no-cache"
        code, _, headers = get(base + "/shared/fonts/manrope-latin.woff2")
        assert code == 200 and headers.get("Content-Type") == "font/woff2"

        # a second Angler on the same data folder refuses to start, without touching the first
        second, log2 = start(home, free_port(), "angler2.log")
        logs.append(log2)
        assert second.wait(timeout=60) == 1, output(home, "angler2.log")
        assert "Another Angler is already running" in output(home, "angler2.log")
        assert get(base + "/api/status")[0] == 200
        assert data_in_use()  # what the PC app waits on before it starts one in place of an old one

        # `baitbook status` sees it ...
        with pytest.raises(SystemExit) as e:
            cli.main(["status"])
        out = capsys.readouterr().out
        assert e.value.code == 0, out
        assert f"Angler  {base}  ·  idle" in out and f"Pairing code  {status['pair_code']}" in out

        # ... and `baitbook stop` stops it
        with pytest.raises(SystemExit) as e:
            cli.main(["stop"])
        out = capsys.readouterr().out
        assert e.value.code == 0 and "The Angler has stopped." in out, out
        assert first.wait(timeout=30) == 0, output(home, "angler.log")
        assert not data_in_use() and not (home / "data" / "angler.port").exists()
    finally:
        stop(second)
        stop(first)
        for log in logs:
            log.close()


def test_status_and_stop_find_an_angler_on_another_port(home, capsys):
    """`baitbook angler --port`: the config keeps its own port, and status and stop still reach it."""
    port = free_port()
    write_config(home, free_port())  # the file names another port, where nothing listens
    base = f"http://127.0.0.1:{port}"
    proc, log = start(home, port, "angler.log")
    try:
        wait_up(proc, base, home, "angler.log")
        with pytest.raises(SystemExit) as e:
            cli.main(["status"])
        out = capsys.readouterr().out
        assert e.value.code == 0 and f"Angler  {base}  ·  idle" in out, out
        with pytest.raises(SystemExit) as e:
            cli.main(["stop"])
        out = capsys.readouterr().out
        assert e.value.code == 0 and "The Angler has stopped." in out, out
        assert proc.wait(timeout=30) == 0, output(home, "angler.log")
    finally:
        stop(proc)
        log.close()


def test_port_in_use(home):
    """An Angler whose port is taken says so and exits 1 (and frees its lock for the next try)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        write_config(home, port)
        proc, log = start(home, port, "angler.log")
        try:
            assert proc.wait(timeout=60) == 1
        finally:
            stop(proc)
            log.close()
    assert "Can't listen on 127.0.0.1" in output(home, "angler.log")
    from angler.__main__ import lock_data

    lock = lock_data()  # the process is gone, and so is its hold on the data folder
    assert lock is not None
    lock.close()
