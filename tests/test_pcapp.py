"""pcapp/__main__.py: opening the PC app on this machine's Angler, and starting one that runs
older code than the app again. Every Angler here is pretend; test_smoke.py has a real one."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

import angler.__main__ as angler_main
import pcapp.__main__ as pcapp
from angler import config

URL = "http://127.0.0.1:8787"


def angler_status(state: str = "idle", queued: tuple = (), **kw) -> dict:
    return {"engine": {"state": state, "queued": list(queued)}, "code": config.code_version(), "background": True, **kw}


@pytest.fixture
def angler(monkeypatch):
    """A pretend Angler on this machine: `.status` is its answer (None: nothing answers). A
    shutdown stops it answering and lets go of the data folder, unless `.holds_on` is set."""
    st = SimpleNamespace(status=angler_status(), holds_on=False, shutdowns=0, started=0, shown=[])

    def urlopen(req, timeout=None):
        assert req.full_url == URL + "/api/shutdown" and req.get_method() == "POST"
        st.shutdowns += 1
        st.status = None
        return io.BytesIO(b'{"ok": true}')

    def start(url):
        st.started += 1
        st.status = angler_status()
        return True

    monkeypatch.setattr(pcapp, "status", lambda url: st.status)
    monkeypatch.setattr(pcapp, "reachable", lambda url: st.status is not None)
    monkeypatch.setattr(pcapp.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(angler_main, "data_in_use", lambda: st.status is not None or st.holds_on)
    monkeypatch.setattr(pcapp, "time", SimpleNamespace(sleep=lambda s: None))
    monkeypatch.setattr(pcapp, "start_angler", start)
    monkeypatch.setattr(pcapp, "show", lambda url, problem="": st.shown.append(url or problem))
    return st


def test_an_angler_on_this_code_is_used_as_it_is(angler):
    pcapp.main()
    assert (angler.shutdowns, angler.started, angler.shown) == (0, 0, [URL + "/app/"])


def test_an_angler_on_another_port_is_found_there(angler):
    """One started with `baitbook angler --port 9000` noted its port; the app opens on it."""
    config.load()
    (config.DATA / "angler.port").write_text("9000", encoding="ascii")
    pcapp.main()
    assert (angler.shutdowns, angler.started, angler.shown) == (0, 0, ["http://127.0.0.1:9000/app/"])


def test_no_angler_starts_one(angler):
    angler.status = None
    pcapp.main()
    assert (angler.shutdowns, angler.started, angler.shown) == (0, 1, [URL + "/app/"])


@pytest.mark.parametrize("status", [
    angler_status(code="0ld"),
    angler_status("error", code="0ld"),  # the last job failed: idle all the same
    {"engine": {"state": "idle", "queued": []}},  # from before an Angler said which code it runs: the PC app started it
])
def test_an_idle_background_angler_on_older_code_is_started_again(angler, capsys, status):
    angler.status = status
    pcapp.main()
    assert (angler.shutdowns, angler.started, angler.shown) == (1, 1, [URL + "/app/"])
    assert "running older code: starting it again" in capsys.readouterr().err


@pytest.mark.parametrize("status, says", [
    (angler_status("editing", code="0ld"), "but busy"),
    (angler_status("idle", queued=[{"kind": "shift"}], code="0ld"), "but busy"),  # a job waits: about to be busy
    (angler_status(code="0ld", background=False), "in a console"),
])
def test_a_busy_or_console_angler_is_left_alone(angler, capsys, status, says):
    angler.status = status
    pcapp.main()
    assert (angler.shutdowns, angler.started, angler.shown) == (0, 0, [URL + "/app/"])  # the app opens on it
    assert says in capsys.readouterr().err


def test_an_angler_that_keeps_the_data_folder_is_not_raced(angler):
    angler.status, angler.holds_on = angler_status(code="0ld"), True
    assert pcapp.replace_if_stale(URL) is False  # gave up after a minute, rather than report it gone


def test_data_in_use_lets_go_at_once():
    config.DATA.mkdir(parents=True, exist_ok=True)
    assert angler_main.data_in_use() is False
    assert angler_main.data_in_use() is False  # asking didn't keep hold of it
    held = angler_main.lock_data()
    try:
        assert angler_main.data_in_use() is True
    finally:
        held.close()
