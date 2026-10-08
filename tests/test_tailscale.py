"""angler/tailscale.py: reading `tailscale status` and `tailscale serve status`, with a pretend `tailscale` command.

The configs below are what `tailscale serve status --json` prints (ipn.ServeConfig).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from angler import tailscale
from tests import conftest

NAME = "pc.tail1234.ts.net"


def serving(proxy: str, hostport: str = f"{NAME}:443", mount: str = "/") -> dict:
    return {"TCP": {hostport.rsplit(":", 1)[1]: {"HTTPS": True}}, "Web": {hostport: {"Handlers": {mount: {"Proxy": proxy}}}}}


@pytest.fixture
def cli(monkeypatch):
    """A pretend `tailscale` command: answers each `tailscale <args> --json` from .answers."""
    st = SimpleNamespace(answers={}, calls=[], code=0)

    def run(argv, **kw):
        st.calls.append(argv)
        out = st.answers.get(tuple(argv[1:-1]))
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(returncode=st.code, stdout=out if isinstance(out, str) else json.dumps(out))

    monkeypatch.setattr(tailscale, "exe", lambda: "tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", run)
    return st


def test_not_installed_answers_none(monkeypatch):
    monkeypatch.setattr(tailscale, "exe", lambda: None)
    assert tailscale.ask("status") is None and tailscale.machine() is None
    assert tailscale.serve_config() is None and tailscale.serve_url(8787) is None


def test_exe_falls_back_to_the_apps_install_folder(monkeypatch, tmp_path):
    app = tmp_path / "tailscale.exe"
    app.write_text("")
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: None)
    monkeypatch.setattr(tailscale, "APP_PATHS", (str(tmp_path / "missing"), str(app)))
    assert conftest.REAL_TAILSCALE_EXE() == str(app)  # the safety net replaced tailscale.exe itself
    monkeypatch.setattr(tailscale, "APP_PATHS", ())
    assert conftest.REAL_TAILSCALE_EXE() is None
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: "/usr/bin/tailscale")
    assert conftest.REAL_TAILSCALE_EXE() == "/usr/bin/tailscale"


@pytest.mark.parametrize("answer, code", [("not json", 0), ({"BackendState": "Running"}, 1),
                                          (subprocess.TimeoutExpired("tailscale", 3), 0), (OSError("gone"), 0)])
def test_ask_gives_up_quietly(cli, answer, code):
    cli.answers[("status",)], cli.code = answer, code
    assert tailscale.ask("status") is None


def test_machine(cli):
    cli.answers[("status",)] = {"BackendState": "Running", "Self": {"DNSName": NAME + "."}, "CertDomains": [NAME]}
    assert tailscale.machine() == tailscale.Machine(running=True, name=NAME, https=True)
    assert cli.calls == [["tailscale", "status", "--json"]]
    cli.answers[("status",)] = {"BackendState": "NeedsLogin", "Self": None, "CertDomains": None}
    assert tailscale.machine() == tailscale.Machine(running=False, name="", https=False)


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:8787", "http://localhost:8787", "127.0.0.1:8787", "http://[::1]:8787"])
def test_serve_url_finds_the_angler(proxy):
    assert tailscale.serve_url(8787, serving(proxy)) == f"https://{NAME}/device/"


def test_serve_url_on_another_https_port():
    assert tailscale.serve_url(8787, serving("http://127.0.0.1:8787", f"{NAME}:8443")) == f"https://{NAME}:8443/device/"


@pytest.mark.parametrize("cfg", [
    {},
    serving("http://127.0.0.1:3000"),                    # something else
    serving("http://127.0.0.1:8787", mount="/bait"),     # the app asks for /api and /shared from the root
    serving("https+insecure://127.0.0.1:8787"),          # not the plain-HTTP Angler
    serving("http://192.168.1.20:8787"),                 # not loopback
    serving("http://127.0.0.1:port"),
    {"Web": {f"{NAME}:443": None}},
])
def test_serve_url_not_this_angler(cfg):
    assert tailscale.serve_url(8787, cfg) is None


def test_serve_url_in_a_foreground_session():
    """`tailscale serve 8787` without --bg: it's there while that console stays open."""
    assert tailscale.serve_url(8787, {"Foreground": {"abc123": serving("http://127.0.0.1:8787")}}) == f"https://{NAME}/device/"


def test_serve_url_asks_tailscale(cli):
    cli.answers[("serve", "status")] = serving("http://127.0.0.1:8788")
    assert tailscale.serve_url(8788) == f"https://{NAME}/device/"
    assert cli.calls == [["tailscale", "serve", "status", "--json"]]


def test_root_taken():
    assert tailscale.root_taken(NAME, 8787, serving("http://127.0.0.1:3000")) == "http://127.0.0.1:3000"
    assert tailscale.root_taken(NAME, 8787, serving("http://127.0.0.1:8787")) is None
    assert tailscale.root_taken(NAME, 8787, serving("http://127.0.0.1:3000", f"{NAME}:8443")) is None
    assert tailscale.root_taken(NAME, 8787, serving("http://127.0.0.1:3000", mount="/grafana")) is None
    assert tailscale.root_taken(NAME, 8787, {}) is None
