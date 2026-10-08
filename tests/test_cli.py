"""baitbook/cli.py (and setup.py, shortcut.py behind it): the `baitbook` command, with no Angler running.

A real Angler for `status` / `stop` to talk to is in test_smoke.py.
"""

from __future__ import annotations

import io
import sys
import tomllib
from types import SimpleNamespace

import pytest

import baitbook
from baitbook import cli
from angler import config
from tests.support import free_port, write_config


def run(argv, capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    return e.value.code, out, err


@pytest.fixture
def port(home):
    """The config points the CLI at a port nothing listens on (never 8787)."""
    p = free_port()
    write_config(home, p)
    return p


def fake_status(state: str = "idle", phase: str = "") -> dict:
    return {"engine": {"state": state, "phase": phase}, "counts": {"residents": 2, "posts": 5, "bites": 3, "snags": 1},
            "pair_code": "123456", "lan_urls": ["http://192.168.1.20:8787/device/"], "code": config.code_version()}


@pytest.fixture
def angler(monkeypatch):
    """A pretend local Angler behind cli.call(): records every call, answers from `.status`."""
    st = SimpleNamespace(calls=[], status=fake_status(), stops_after=None)

    def call(path, method="GET", timeout=3):
        st.calls.append((method, path))
        if method == "POST" and path == "/api/shutdown":
            st.stops_after = st.stops_after if st.stops_after is not None else 1
            return {"ok": True}
        if st.stops_after is not None:
            st.stops_after -= 1
            if st.stops_after < 0:
                return None
        return st.status

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda s: None))
    return st


# ---------- argument parsing ----------
def test_version(capsys):
    code, out, _ = run(["--version"], capsys)
    assert code == 0 and out.strip() == f"baitbook {baitbook.__version__}"


def test_help(capsys):
    code, out, _ = run(["--help"], capsys)
    assert code == 0
    for command in ("app", "angler", "stop", "status", "phone", "setup", "shortcut"):
        assert command in out


@pytest.mark.parametrize("argv", [["nonsense"], ["angler", "--port", "eighty"], ["stop", "--now"], ["status", "extra"], ["--nope"]])
def test_bad_arguments(capsys, argv):
    code, _, err = run(argv, capsys)
    assert code == 2 and "usage: baitbook" in err


def test_no_command_opens_the_app(capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "cmd_app", lambda args: opened.append(args) or 0)
    assert run([], capsys)[0] == 0
    assert len(opened) == 1


@pytest.mark.parametrize("argv, passed", [
    (["angler"], []),
    (["angler", "--port", "9999"], ["--port", "9999"]),
    (["angler", "--host", "127.0.0.1", "--port", "9999"], ["--host", "127.0.0.1", "--port", "9999"]),
])
def test_angler_passes_its_options_on(capsys, monkeypatch, argv, passed):
    import angler.__main__ as angler_main

    got = []
    monkeypatch.setattr(angler_main, "main", lambda argv=None: got.append(argv))
    assert run(argv, capsys)[0] == 0
    assert got == [passed]


def test_angler_main_parses_the_options_it_is_given(monkeypatch):
    """`baitbook angler --port N` hands argv to angler.__main__.main(argv), which must not read sys.argv."""
    import angler.__main__ as angler_main

    class Parsed(Exception):
        pass

    def stop_here():  # config.load() is the first thing after the arguments: stop there
        raise Parsed

    monkeypatch.setattr(sys, "argv", ["pytest", "--definitely-not-an-angler-option"])
    monkeypatch.setattr(angler_main.config, "load", stop_here)
    with pytest.raises(Parsed):
        angler_main.main(["--port", "9999"])
    with pytest.raises(SystemExit) as e:
        angler_main.main(["--port", "nine"])
    assert e.value.code == 2


# ---------- status / stop with nothing running ----------
def test_status_with_no_angler(capsys, port, home):
    code, out, _ = run(["status"], capsys)
    assert code == 1
    assert f"Config  {home / 'baitbook.toml'}" in out and f"Data    {home / 'data'}" in out
    assert "Angler  not running" in out


def test_stop_with_no_angler(capsys, port):
    code, out, _ = run(["stop"], capsys)
    assert code == 0 and out.strip() == "No Angler is running on this machine."


def test_local_url(port):
    assert cli.local_url() == f"http://127.0.0.1:{port}"
    config.update({"device": {"keep_nights": 3}})  # (unrelated edits keep the port)
    assert cli.local_url() == f"http://127.0.0.1:{port}"


def test_local_url_follows_an_angler_on_another_port(port):
    """One started with `baitbook angler --port`: status and stop look where it listens."""
    from angler.__main__ import lock_data

    config.load()
    other = free_port()
    (config.DATA / "angler.port").write_text(str(other), encoding="ascii")
    assert cli.local_url() == f"http://127.0.0.1:{port}"  # nothing holds the data folder: a stale note
    held = lock_data()
    try:
        assert cli.local_url() == f"http://127.0.0.1:{other}"
    finally:
        held.close()
    assert cli.local_url() == f"http://127.0.0.1:{port}"


# ---------- status / stop against a pretend Angler ----------
def test_status_running(capsys, port, angler):
    code, out, _ = run(["status"], capsys)
    assert code == 0
    assert f"Angler  http://127.0.0.1:{port}  ·  idle" in out
    assert "2 residents · 5 edits · 3 bites · 1 snags" in out
    assert "Pairing code  123456" in out and "Phone   http://192.168.1.20:8787/device/  (same Wi-Fi only" in out
    assert "For offline mode on the phone: `baitbook phone`" in out
    assert "older code" not in out


@pytest.mark.parametrize("code", ["0ld", None])  # None: an Angler from before it reported its code
def test_status_says_when_the_angler_runs_older_code(capsys, port, angler, code):
    angler.status["code"] = code
    if code is None:
        del angler.status["code"]
    out = run(["status"], capsys)[1]
    assert "running older code than this: `baitbook stop`, then `baitbook`" in out


def test_status_with_tailscale_serve(capsys, port, angler):
    angler.status["tailscale_url"] = "https://pc.tailnet.ts.net/device/"
    code, out, _ = run(["status"], capsys)
    assert code == 0
    lines = out.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("Phone"))
    assert lines[first] == "Phone   https://pc.tailnet.ts.net/device/  (through Tailscale: works offline)"
    assert lines[first + 1].startswith("Phone   http://192.168.1.20:8787/device/")
    assert "baitbook phone" not in out


def test_status_with_its_own_certificate(capsys, port, angler):
    angler.status["lan_urls"] = ["https://192.168.1.20:8787/device/"]
    code, out, _ = run(["status"], capsys)
    assert code == 0 and "Phone   https://192.168.1.20:8787/device/\n" in out and "baitbook phone" not in out


def test_stop(capsys, port, angler):
    code, out, _ = run(["stop"], capsys)
    assert code == 0 and "The Angler has stopped." in out
    assert ("POST", "/api/shutdown") in angler.calls


@pytest.mark.parametrize("state", ["starting", "reading", "writing", "editing"])
def test_stop_refuses_while_busy(capsys, port, angler, state):
    angler.status = fake_status(state, "Phase 1 of 2 · writing prompts")
    code, out, _ = run(["stop"], capsys)
    assert code == 1 and "busy (Phase 1 of 2 · writing prompts)" in out and "--force" in out
    assert ("POST", "/api/shutdown") not in angler.calls


def test_stop_after_a_failed_job_is_not_busy(capsys, port, angler):
    angler.status = fake_status("error")
    assert run(["stop"], capsys)[0] == 0


def test_stop_force(capsys, port, angler):
    angler.status = fake_status("editing")
    code, out, _ = run(["stop", "--force"], capsys)
    assert code == 0 and "The Angler has stopped." in out
    assert ("POST", "/api/shutdown") in angler.calls


def test_stop_that_never_happens(capsys, port, angler):
    angler.stops_after = 10**6
    code, _, err = run(["stop"], capsys)
    assert code == 1 and "didn't stop" in err
    assert sum(1 for c in angler.calls if c == ("GET", "/api/status")) == 61  # the first look, then 60 tries


# ---------- setup ----------
@pytest.fixture
def no_questions(monkeypatch):
    def ask(*a, **k):
        raise AssertionError(f"setup asked a question: {a}")

    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)  # not installed, wherever the tests run


def test_setup_check_on_a_fresh_config(capsys, home, no_questions):
    code, out, _ = run(["setup", "--check"], capsys)
    assert code == 1
    assert "llama-cpp-python isn't installed" in out
    assert "[text] model" in out and "[text] mmproj" in out and "ComfyUI" in out
    assert "still missing" in out
    assert (home / "baitbook.toml").read_bytes() == config.EXAMPLE_PATH.read_bytes()  # created, but nothing changed


def test_setup_without_a_terminal_asks_nothing(capsys, home, no_questions, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert run(["setup"], capsys)[0] == 1


def install_everything(home, monkeypatch, gguf: bool = False) -> None:
    """What a finished setup has: llama-cpp-python, both GGUFs, ComfyUI with its Python and model files."""
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(__version__="0.3.36", llama_supports_gpu_offload=lambda: True))
    (home / "models").mkdir()
    for f in ("gemma.gguf", "mmproj.gguf"):
        (home / "models" / f).write_bytes(b"GGUF")
    comfy = home / "ComfyUI"
    (comfy / "venv" / "bin").mkdir(parents=True)
    (comfy / "main.py").write_text("", encoding="utf-8")
    (comfy / "venv" / "bin" / "python").write_bytes(b"")
    image = config.load()["image"]
    if gguf:
        image.update(unet="klein-9b-Q8_0.gguf", text_encoder="enc-q8_0.gguf")
        config.update({"image": {"unet": image["unet"], "text_encoder": image["text_encoder"]}})
    for key, folder in (("unet", "diffusion_models"), ("text_encoder", "text_encoders"), ("vae", "vae")):
        (comfy / "models" / folder).mkdir(parents=True)
        (comfy / "models" / folder / image[key]).write_bytes(b"x")


def test_setup_check_all_set(capsys, home, monkeypatch, no_questions):
    install_everything(home, monkeypatch)
    # relative paths are relative to baitbook.toml
    config.update({"text": {"model": "models/gemma.gguf", "mmproj": "models/mmproj.gguf"},
                   "image": {"comfy_dir": "ComfyUI", "comfy_python": "ComfyUI/venv/bin/python"}}, local=True)
    code, out, _ = run(["setup", "--check"], capsys)
    assert code == 0, out
    assert "All set" in out and "!!" not in out
    assert "llama-cpp-python 0.3.36 (GPU)" in out


def test_setup_check_gguf_needs_the_custom_nodes(capsys, home, monkeypatch, no_questions):
    install_everything(home, monkeypatch, gguf=True)
    config.update({"text": {"model": "models/gemma.gguf", "mmproj": "models/mmproj.gguf"},
                   "image": {"comfy_dir": "ComfyUI", "comfy_python": "ComfyUI/venv/bin/python"}}, local=True)
    code, out, _ = run(["setup", "--check"], capsys)
    assert code == 1 and "ComfyUI-GGUF" in out
    (home / "ComfyUI" / "custom_nodes" / "ComfyUI-GGUF").mkdir(parents=True)
    assert run(["setup", "--check"], capsys)[0] == 0


def test_setup_asks_and_saves_the_local_only_paths(capsys, home, monkeypatch):
    install_everything(home, monkeypatch)
    answers = iter(["n",  # not baitbook's own ComfyUI: one of ours
                    str(home / "ComfyUI"), "y",  # ComfyUI, and yes to the Python found inside it
                    "n",  # don't download the default models
                    f'"{home / "models" / "nope.gguf"}"',  # not there: asked again
                    f'"{home / "models" / "gemma.gguf"}"', str(home / "models" / "mmproj.gguf"),
                    "n"])  # no app icon
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or next(answers))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    code, out, _ = run(["setup"], capsys)
    assert code == 0, out
    assert len(asked) == 8 and "not found there" in out
    assert any("install ComfyUI" in q for q in asked)
    saved = tomllib.loads((home / "baitbook.toml").read_text(encoding="utf-8"))
    assert saved["text"]["model"] == str((home / "models" / "gemma.gguf").resolve())
    assert saved["image"]["comfy_dir"] == str((home / "ComfyUI").resolve())
    assert saved["image"]["comfy_python"] == str((home / "ComfyUI" / "venv" / "bin" / "python").resolve())
    assert not (home.parent / "shortcuts").exists()  # said no to the app icon


# ---------- shortcut (into BAITBOOK_SHORTCUT_DIR, never the real menus) ----------
def test_shortcut_create_and_remove(capsys, tmp_path):
    folder = tmp_path / "shortcuts"
    code, out, _ = run(["shortcut", "--desktop"], capsys)
    assert code == 0
    added = [line.removeprefix("added ") for line in out.splitlines()]
    assert len(added) == 2 and all(line.startswith(str(folder)) for line in added)
    assert any((folder / "Desktop").iterdir())
    menu = next(p for p in folder.iterdir() if p.name != "Desktop")
    assert menu.name.startswith("baitbook")
    if sys.platform.startswith("linux"):
        entry = menu.read_text(encoding="utf-8")
        assert "[Desktop Entry]" in entry and "Exec=" in entry and "baitbook" in entry

    code, out, _ = run(["shortcut", "--remove"], capsys)
    assert code == 0 and out.count("removed ") == 2
    assert run(["shortcut", "--remove"], capsys)[1].strip() == "nothing to remove"


# ---------- phone: Tailscale Serve ----------
NAME = "pc.tail1234.ts.net"


@pytest.fixture
def ts(monkeypatch, port):
    """A pretend Tailscale behind angler.tailscale, and `tailscale serve` behind cli.subprocess.run."""
    from angler import tailscale

    st = SimpleNamespace(exe="tailscale", machine=tailscale.Machine(running=True, name=NAME, https=True),
                         config={}, runs=[], serve_ok=True, target=f"http://127.0.0.1:{port}")

    def run(argv, **kw):
        st.runs.append(argv)
        if st.serve_ok:
            st.config = {"Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": argv[-1]}}}}}
        return SimpleNamespace(returncode=0 if st.serve_ok else 1)

    monkeypatch.setattr(tailscale, "exe", lambda: st.exe)
    monkeypatch.setattr(tailscale, "machine", lambda: st.machine)
    monkeypatch.setattr(tailscale, "serve_config", lambda: st.config)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "call", lambda path, method="GET", timeout=3: fake_status())
    return st


def test_phone_sets_up_serve(capsys, ts):
    code, out, _ = run(["phone"], capsys)
    assert code == 0
    assert ts.runs == [["tailscale", "serve", "--bg", ts.target]]
    assert f"The phone app is at\n\n  https://{NAME}/device/" in out
    assert "█" in out  # the QR code
    assert "Install Tailscale" in out and "pair with the code 123456" in out


def test_phone_when_already_serving(capsys, ts):
    ts.config = {"Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": ts.target}}}}}
    code, out, _ = run(["phone"], capsys)
    assert code == 0 and ts.runs == []
    assert "already passes this Angler on" in out and f"https://{NAME}/device/" in out


def test_phone_with_https_off_lets_tailscale_ask(capsys, ts):
    ts.machine.https = False
    code, out, _ = run(["phone"], capsys)
    assert code == 0 and len(ts.runs) == 1
    assert "HTTPS isn't on for your tailnet yet" in out


def test_phone_when_serve_fails(capsys, ts):
    ts.serve_ok = False
    code, out, err = run(["phone"], capsys)
    assert code == 1 and "didn't finish" in err and "login.tailscale.com/admin/dns" in err
    assert "The phone app is at" not in out


def test_phone_never_replaces_another_served_site(capsys, ts):
    ts.config = {"Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    code, out, _ = run(["phone"], capsys)
    assert code == 1 and ts.runs == []
    assert "http://127.0.0.1:3000" in out and f"tailscale serve --bg --https=8443 {ts.target}" in out


@pytest.mark.parametrize("what, said", [
    ("not installed", "isn't installed"), ("not connected", "isn't connected"), ("no answer", "isn't connected"),
    ("no name", "Turn on MagicDNS"),
])
def test_phone_needs_tailscale_first(capsys, ts, what, said):
    from angler import tailscale

    if what == "not installed":
        ts.exe = None
    elif what == "not connected":
        ts.machine = tailscale.Machine(running=False, name="", https=False)
    elif what == "no answer":
        ts.machine = None
    else:
        ts.machine = tailscale.Machine(running=True, name="", https=False)
    code, out, _ = run(["phone"], capsys)
    assert code == 1 and said in out and "`baitbook phone` again" in out
    assert ts.runs == []


def test_phone_with_no_angler_running(capsys, ts, monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *a, **k: None)
    code, out, _ = run(["phone"], capsys)
    assert code == 0 and "the code the PC app shows in Settings (start it with `baitbook`)" in out


@pytest.mark.parametrize("argv, host, port", [
    ([], "0.0.0.0", 8787),
    (["--port", "9000"], "0.0.0.0", 9000),
    (["--host", "127.0.0.1", "--port", "9000"], "127.0.0.1", 9000),
])
def test_angler_options_reach_the_config(monkeypatch, argv, host, port):
    """--host / --port are what /api/status's phone addresses and Tailscale check read, and they
    survive the server's startup loading the file again; the file itself keeps 8787."""
    import angler.__main__ as angler_main

    monkeypatch.setattr(angler_main, "lock_data", lambda: None)  # "another Angler is running": stop before binding
    with pytest.raises(SystemExit):
        angler_main.main(argv)
    config.load()
    assert (config.section("server")["host"], config.section("server")["port"]) == (host, port)
    assert tomllib.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["server"]["port"] == 8787
