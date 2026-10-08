"""The baitbook PC app: a native window onto an Angler.

`baitbook` (or `baitbook-app`, with no console). With [app] angler_url empty it uses the Angler on this
machine, over plain HTTP on 127.0.0.1 even when the Angler serves HTTPS, and
starts it in the background if it isn't up; the Angler keeps running after
the window closes, so scheduled night shifts still happen. One it started that
runs older code than this (a source checkout changed, an upgrade installed since)
is started again first, if it's idle.
With angler_url pointing at another machine, the app pairs with that Angler
as the owner the first time; the window keeps its storage in data/webview so
the token survives a restart. With no native window to be had (Linux without
GTK or Qt, say) it opens the app in the browser instead.
"""

from __future__ import annotations

import html
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from angler import config

ICONS = Path(__file__).resolve().parents[1] / "baitbook" / "icons"
LOG_MAX = 5_000_000  # data/angler.log starts over (the last one kept as angler.log.1) past this


def reachable(url: str) -> bool:
    """An Angler answers at url: 200, or 401 from one that wants a token."""
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:  # urlopen raises for a 401 too
        return exc.code == 401
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    cfg = config.load()
    url = (cfg.get("app", {}).get("angler_url") or "").rstrip("/")
    if not url:
        # with HTTPS on, the Angler's loopback-only [server] local_port: no cert names 127.0.0.1.
        # One running on another port (`baitbook angler --port`) is found there.
        url = f"http://127.0.0.1:{config.angler_port(cfg['server'])}"
        replace_if_stale(url)
        if not reachable(url) and not start_angler(url):
            return show(None, f"The Angler didn't start. Its log says why: {config.DATA / 'angler.log'}")
    show(url + "/app/")


def status(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def replace_if_stale(url: str) -> bool:
    """Stop the Angler on this machine if it runs other code than this app (it outlives the
    window, so it would otherwise keep running a source checkout's old code, or the version
    before an upgrade, until something stops it), so main() starts this code in its place.
    Only one started in the background, and only while idle: a job or a console is left alone,
    and the PC app shows it's out of date. True if it was stopped."""
    st = status(url)
    if st is None or st.get("code") == config.code_version():
        return False
    e = st["engine"]
    # an Angler from before `code` and `background` were reported is old by definition, and the PC app started it
    if not st.get("background", "code" not in st):
        print("The Angler running in a console is older than its code: stop it there (Ctrl+C) and start it again.", file=sys.stderr)
        return False
    if e["state"] not in ("idle", "error") or e.get("queued"):
        print("The Angler is older than its code, but busy; `baitbook stop` and `baitbook` once it's idle.", file=sys.stderr)
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(url + "/api/shutdown", data=b"", method="POST"), timeout=3).close()
    except Exception:  # noqa: BLE001
        return False
    from angler.__main__ import data_in_use

    # it answers, stops listening, then lets go of the data folder (after stopping a ComfyUI it started)
    for _ in range(120):
        if not reachable(url) and not data_in_use():
            print("The Angler was running older code: starting it again.", file=sys.stderr)
            return True
        time.sleep(0.5)
    return False


def start_angler(url: str) -> bool:
    """Start the Angler in the background, detached so it outlives this window (and the
    console it came from), and wait until it answers. False if it quit or never did."""
    path = config.DATA / "angler.log"
    if path.exists() and path.stat().st_size > LOG_MAX:
        path.replace(path.with_name("angler.log.1"))
    log = open(path, "ab")
    detach = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if sys.platform == "win32"
              else {"start_new_session": True})
    # cwd: where the angler package is, so `-m angler` finds it from a source checkout too
    angler = subprocess.Popen([sys.executable, "-m", "angler", "--background"], cwd=config.ROOT, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, **detach)
    for _ in range(60):
        if reachable(url):
            return True
        if angler.poll() is not None:  # it quit (another Angler on this data, a taken port, ...)
            return False
        time.sleep(1)
    return False


def show(url: str | None, problem: str = "") -> None:
    """The app in a native window, or `problem` in one; the browser if there's no window to be had."""
    try:
        import webview

        storage = config.DATA / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        page = {"url": url} if url else {"html": f"<body style='font:15px system-ui;padding:40px;background:#F4EDE1'>{html.escape(problem)}</body>"}
        webview.create_window("baitbook", width=1440, height=920, min_size=(900, 640), background_color="#F4EDE1", **page)
        icon = ICONS / ("baitbook.ico" if sys.platform == "win32" else "baitbook.png")  # macOS: the app bundle's
        webview.start(private_mode=False, storage_path=str(storage), icon=str(icon) if icon.is_file() else None)
    except Exception as exc:  # noqa: BLE001
        if not url:
            print(problem, file=sys.stderr)
            return
        print(f"No app window here ({exc}); opening {url} in the browser.", file=sys.stderr)
        webbrowser.open(url)


if __name__ == "__main__":
    main()
