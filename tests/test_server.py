"""angler/server.py through FastAPI's TestClient: who's local, pairing, tokens, the dock, the web files.

TestClient's default peer is "testclient", which is never local. `api.owner` is
this machine (peer 127.0.0.1, Host 127.0.0.1); `api.remote` is a phone on the
Wi-Fi (peer 192.168.1.50, Host 192.168.1.20:8787).
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from angler import config, db, media, server
from angler.engine import Engine
from tests.support import ROOT, add_edit, add_resident, png, wait_for, wait_idle


@pytest.fixture
def api(fakes, monkeypatch):
    monkeypatch.setattr(server, "_pair", {"code": "", "fails": 0, "paused_until": 0.0})
    # resolving this machine's own name can take seconds on some hosts (macOS runners); lan_urls has its own test
    monkeypatch.setattr(server, "lan_urls", lambda: ["http://192.168.1.20:8787/device/"])
    monkeypatch.setattr(server, "tailscale_url", lambda: "https://pc.tailnet.ts.net/device/")
    with TestClient(server.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as owner:  # Host 127.0.0.1
        eng = server.engine
        remote = TestClient(server.app, base_url="http://192.168.1.20:8787", client=("192.168.1.50", 50001))
        lm, comfy = fakes
        yield SimpleNamespace(owner=owner, remote=remote, engine=eng, lm=lm, comfy=comfy)
        wait_idle(eng)


def pair(api, role: str = "device", name: str | None = None) -> str:
    body = {"code": server.pair_code(), "role": role}
    if name:
        body["name"] = name
    r = api.remote.post("/api/pair", json=body)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def tok(token: str) -> dict:
    return {"x-baitbook-token": token}


# ---------- is_local ----------
def request(peer: str | None, host: str | None, **headers) -> Request:
    hs = [(b"host", host.encode())] if host is not None else []
    hs += [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": hs,
                    "client": (peer, 5555) if peer else None})


@pytest.mark.parametrize("peer, host", [
    ("127.0.0.1", "127.0.0.1:8787"), ("127.0.0.1", "127.0.0.1"), ("127.0.0.1", "localhost:8787"), ("127.0.0.1", "LOCALHOST"),
    ("::1", "[::1]:8787"), ("::1", "[::1]"), ("::1", "localhost"), ("localhost", "127.0.0.1:8788"),
])
def test_is_local_loopback(peer, host):
    assert server.is_local(request(peer, host)) is True


@pytest.mark.parametrize("peer, host", [
    ("192.168.1.50", "127.0.0.1:8787"),  # a phone, whatever Host it sends
    ("testclient", "127.0.0.1"),
    ("127.0.0.1", "192.168.1.20:8787"),  # loopback peer, but asked for by a LAN name
    ("127.0.0.1", "pc.tailnet.ts.net"),
    ("127.0.0.1", "127.0.0.1.evil.example"),
    ("127.0.0.1", "localhost.evil.example:8787"),
    ("127.0.0.1", ""),
    ("127.0.0.1", None),  # no Host at all
    ("::ffff:127.0.0.1", "127.0.0.1"),  # not on the list: refused rather than guessed
    (None, "127.0.0.1"),  # no peer
])
def test_is_local_refuses(peer, host):
    assert server.is_local(request(peer, host)) is False


@pytest.mark.parametrize("header", server.PROXY_HEADERS)
def test_is_local_refuses_proxied_requests(header):
    """`tailscale serve` (or any reverse proxy on this machine) connects from 127.0.0.1 too."""
    assert server.is_local(request("127.0.0.1", "127.0.0.1:8787", **{header: "x"})) is False
    assert server.is_local(request("127.0.0.1", "127.0.0.1:8787", **{header.upper(): "x"})) is False


@pytest.mark.parametrize("headers", [
    {"origin": "https://untrusted.example"},
    {"origin": "null"},  # a sandboxed frame, a file:// page
    {"origin": "http://localhost:3000"},  # another server on this machine is another origin
    {"origin": "http://127.0.0.1:8788"},  # so is another port
    {"sec_fetch_site": "cross-site"},
    {"sec_fetch_site": "same-site"},  # localhost:3000 to localhost:8787 is the same site, not the same origin
    {"sec_fetch_site": "cross-site", "origin": "http://127.0.0.1:8787"},
])
def test_is_local_refuses_pages_from_elsewhere(headers):
    """Any page in a browser here can send this machine a form or a simple fetch; the browser
    only keeps it from reading the answer."""
    assert server.is_local(request("127.0.0.1", "127.0.0.1:8787", **headers)) is False


@pytest.mark.parametrize("host, headers", [
    ("127.0.0.1:8787", {"origin": "http://127.0.0.1:8787", "sec_fetch_site": "same-origin"}),  # the PC app's own page
    ("localhost:8787", {"origin": "http://LOCALHOST:8787"}),
    ("[::1]:8787", {"origin": "http://[::1]:8787"}),
    ("127.0.0.1:8787", {"sec_fetch_site": "none"}),  # an address typed in
    ("127.0.0.1:8787", {}),  # the CLI
])
def test_is_local_takes_the_anglers_own_pages(host, headers):
    assert server.is_local(request("127.0.0.1", host, **headers)) is True


# ---------- who's calling ----------
def test_owner_status(api):
    s = api.owner.get("/api/status").json()
    assert s["owner"] is True and s["you"] == "pc"
    assert s["pair_code"] == server.pair_code() and re.fullmatch(r"\d{6}", s["pair_code"])
    assert s["devices"] == [] and isinstance(s["lan_urls"], list) and s["tailscale_url"] == "https://pc.tailnet.ts.net/device/"
    assert s["models"] == {"text": "Gemma 4 31B", "image": "FLUX.2 [klein] 4B"}
    assert s["engine"]["state"] == "idle" and s["counts"]["residents"] == 0


def test_status_says_when_its_code_changed_since_it_started(api, monkeypatch):
    s = api.owner.get("/api/status").json()
    assert s["code"] == server.CODE == config.code_version() and s["stale"] is False and s["background"] is False
    monkeypatch.setattr(config, "code_version", lambda: "changed")
    monkeypatch.setattr(server.app.state, "background", True, raising=False)  # `angler --background`, from the PC app
    s = api.owner.get("/api/status").json()
    assert s["code"] == server.CODE and s["stale"] is True and s["background"] is True


def test_code_version_follows_the_anglers_python(monkeypatch, tmp_path):
    pkg = tmp_path / "angler"
    (pkg / "workflows").mkdir(parents=True)
    (pkg / "server.py").write_text("x = 1\n")
    (pkg / "workflows" / "klein_i2i.json").write_text("{}")
    monkeypatch.setattr(config, "PKG", pkg)
    before = config.code_version()
    (pkg / "workflows" / "klein_i2i.json").write_text('{"read": "every job"}')
    assert config.code_version() == before
    (pkg / "server.py").write_text("x = 2\n")
    assert config.code_version() != before


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/status"), ("GET", "/api/config"), ("GET", "/api/residents"), ("GET", "/api/posts"), ("GET", "/api/posts/1"),
    ("GET", "/api/shifts"), ("GET", "/api/journal"), ("POST", "/api/votes"), ("POST", "/api/dock"), ("POST", "/api/unpair"),
    ("GET", "/media/x.jpg"), ("PUT", "/api/config"), ("POST", "/api/shift"), ("GET", "/api/residents/moss/graph"),
])
def test_no_token_is_401(api, method, path):
    r = api.remote.request(method, path, json={})
    assert r.status_code == 401 and r.json() == {"error": "pair this device first"}
    assert api.remote.request(method, path, json={}, headers=tok("not-a-real-token")).status_code == 401


def test_a_proxied_request_from_loopback_needs_a_token(api):
    ts = TestClient(server.app, base_url="https://pc.tailnet.ts.net", client=("127.0.0.1", 50002),
                    headers={"X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "me@example.com"})
    assert ts.get("/api/status").status_code == 401
    token = pair(api)
    s = ts.get("/api/status", headers=tok(token)).json()
    assert s["owner"] is False and s["pair_code"] is None
    assert ts.put("/api/config", json={}, headers=tok(token)).status_code == 403
    # and a loopback peer that names a LAN address (DNS rebinding, a misrouted proxy) isn't local either
    lan = TestClient(server.app, base_url="http://192.168.1.20:8787", client=("127.0.0.1", 50003))
    assert lan.get("/api/status").status_code == 401


CROSS_SITE = {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}


def test_a_page_elsewhere_gets_nothing_of_the_owners(api, monkeypatch):
    """The audit's case: a page on another site has this machine's browser post a form here."""
    calls = []
    monkeypatch.setattr(api.engine, "submit", lambda *a, **k: calls.append(a) or {"ok": True})
    stopped = threading.Event()
    monkeypatch.setattr(server.app.state, "stop", stopped.set, raising=False)
    form = {**CROSS_SITE, "Content-Type": "application/x-www-form-urlencoded"}
    assert api.owner.post("/api/shift", headers=form, content="").status_code == 401
    assert api.owner.post("/api/shutdown", headers=form, content="").status_code == 403
    r = api.owner.post("/api/residents", headers=CROSS_SITE, data={"name": "x", "handle": "x"}, files=[("images", ("a.png", png(), "image/png"))])
    assert r.status_code == 401
    time.sleep(0.5)  # the stop timer would have fired by now
    assert calls == [] and not stopped.is_set() and db.resident("x") is None
    # the PC app's own page is still the owner
    own = {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    assert api.owner.post("/api/shift", headers=own).json() == {"ok": True} and len(calls) == 1


def test_device_token(api):
    token = pair(api, name="Pixel")
    s = api.remote.get("/api/status", headers=tok(token)).json()
    assert s["owner"] is False and s["you"] == "Pixel"
    assert s["pair_code"] is None and s["devices"] is None and s["lan_urls"] is None and s["tailscale_url"] is None
    assert api.remote.get(f"/api/status?t={token}").status_code == 200  # as a query parameter too (image URLs)
    assert api.remote.get("/api/config", headers=tok(token)).status_code == 200


@pytest.mark.parametrize("method, path, kw", [
    ("PUT", "/api/config", {"json": {"shift": {"posts_per_resident": 1}}}),
    ("POST", "/api/shift", {}),
    ("DELETE", "/api/devices/1", {}),
    ("PATCH", "/api/residents/moss", {"json": {"name": "x"}}),
    ("DELETE", "/api/residents/moss", {}),
    ("POST", "/api/residents/moss/read", {}),
    ("POST", "/api/residents", {"data": {"name": "x", "handle": "x"}, "files": [("images", ("a.png", png(), "image/png"))]}),
    ("POST", "/api/shutdown", {}),
])
def test_device_gets_403_on_owner_routes(api, method, path, kw):
    add_resident("moss")
    token = pair(api)
    r = api.remote.request(method, path, headers=tok(token), **kw)
    assert r.status_code == 403, r.text
    assert db.resident("moss") is not None and db.resident("x") is None
    assert config.get()["shift"]["posts_per_resident"] == 5


def test_owner_role_token_passes_owner_routes(api):
    token = pair(api, role="owner")
    s = api.remote.get("/api/status", headers=tok(token)).json()
    assert s["owner"] is True and s["you"] == "PC" and s["pair_code"] == server.pair_code()
    r = api.remote.put("/api/config", json={"shift": {"posts_per_resident": 2}}, headers=tok(token))
    assert r.status_code == 200 and r.json()["shift"]["posts_per_resident"] == 2
    assert api.remote.post("/api/shift", headers=tok(token)).json() == {"ok": True}


def test_unpair_and_remove_device(api):
    phone, tablet = pair(api, name="phone"), pair(api, name="tablet")
    assert api.remote.post("/api/unpair", headers=tok(phone)).json() == {"ok": True}
    assert api.remote.get("/api/status", headers=tok(phone)).status_code == 401
    devices = api.owner.get("/api/status").json()["devices"]
    assert [d["name"] for d in devices] == ["tablet"]
    r = api.owner.delete(f"/api/devices/{devices[0]['id']}")
    assert r.status_code == 200 and r.json()["devices"] == []
    assert "removed paired device tablet." in [e["text"] for e in db.events()]
    assert api.remote.get("/api/status", headers=tok(tablet)).status_code == 401
    assert api.owner.post("/api/unpair").json() == {"ok": True}  # this machine has no token to forget


def test_removing_a_device_by_a_stale_id_never_removes_another(api):
    """The audit's case: the last device removed, another paired, then the old "remove" sent again."""
    pair(api, name="old")
    old_id = api.owner.get("/api/status").json()["devices"][0]["id"]
    assert api.owner.delete(f"/api/devices/{old_id}").status_code == 200
    new = pair(api, name="new")
    assert api.owner.delete(f"/api/devices/{old_id}").status_code == 404
    assert api.remote.get("/api/status", headers=tok(new)).status_code == 200


def test_lan_urls(home, monkeypatch):
    import socket

    addrs = ["127.0.0.1", "192.168.1.20", "169.254.3.4", "10.0.0.5", "192.168.1.20"]
    lookups = []

    def getaddrinfo(*a, **k):
        lookups.append(a)
        return [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in addrs]

    monkeypatch.setattr(server.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(server, "route_ip", lambda: None)  # can't tell which adapter: every address
    monkeypatch.setattr(server, "_ips", (0.0, []))
    config.load()
    assert server.lan_urls() == ["http://10.0.0.5:8787/device/", "http://192.168.1.20:8787/device/"]
    config._cfg["server"]["ssl_certfile"] = "cert.pem"
    assert server.lan_urls()[0] == "https://10.0.0.5:8787/device/"
    assert len(lookups) == 1  # kept for a minute: /api/status is polled

    def offline(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(server.socket, "getaddrinfo", offline)
    monkeypatch.setattr(server, "_ips", (0.0, []))
    assert server.lan_urls() == []


def test_lan_urls_keep_the_adapter_with_the_route(home, monkeypatch):
    """WSL's, Hyper-V's and Docker's virtual adapters, and Tailscale's, are no use to a phone on the Wi-Fi."""
    import socket

    addrs = ["172.30.16.1", "192.168.1.20", "100.64.0.7"]
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in addrs])
    monkeypatch.setattr(server, "route_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(server, "_ips", (0.0, []))
    config.load()
    assert server.lan_urls() == ["http://192.168.1.20:8787/device/"]
    # a route through an address that isn't this machine's own (a VPN's, say): every address but Tailscale's
    monkeypatch.setattr(server, "route_ip", lambda: "10.8.0.2")
    monkeypatch.setattr(server, "_ips", (0.0, []))
    assert server.lan_urls() == ["http://172.30.16.1:8787/device/", "http://192.168.1.20:8787/device/"]


def test_route_ip_sends_nothing():
    ip = server.route_ip()  # None on a machine with no route at all
    assert ip is None or (server.ipaddress.ip_address(ip).version == 4 and not ip.startswith("127."))


def test_tailscale_url(home, monkeypatch):
    asked = []
    monkeypatch.setattr(server.tailscale, "serve_url", lambda port: asked.append(port) or "https://pc.tailnet.ts.net/device/")
    monkeypatch.setattr(server, "_serve", (0.0, None))
    config.load()
    assert server.tailscale_url() == "https://pc.tailnet.ts.net/device/"
    assert server.tailscale_url() == "https://pc.tailnet.ts.net/device/"
    assert asked == [8787]  # kept for 30 s: /api/status is polled
    config._cfg["server"]["ssl_certfile"] = "cert.pem"  # with HTTPS on, Serve passes on to the loopback port
    monkeypatch.setattr(server, "_serve", (0.0, None))
    server.tailscale_url()
    assert asked == [8787, 8788]


def test_tailscale_url_without_tailscale(home, monkeypatch):
    monkeypatch.setattr(server, "_serve", (0.0, None))
    config.load()
    assert server.tailscale_url() is None


# ---------- pairing ----------
def test_pair(api):
    r = api.remote.post("/api/pair", json={"code": f" {server.pair_code()} ", "name": "<b>My Phone!</b>"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "device" and body["name"] == "bMy Phoneb" and len(body["token"]) >= 30
    assert db.device(body["token"])["role"] == "device"
    owner = api.remote.post("/api/pair", json={"code": server.pair_code(), "role": "owner"}).json()
    assert owner["role"] == "owner" and owner["name"] == "PC"
    other = api.remote.post("/api/pair", json={"code": server.pair_code(), "role": "admin", "name": "x" * 99}).json()
    assert other["role"] == "device" and other["name"] == "x" * 40
    assert api.remote.post("/api/pair", json={"code": server.pair_code(), "name": "!!!"}).json()["name"] == "phone"


def test_pair_wrong_code_five_times_changes_the_code_and_pauses(api):
    code = server.pair_code()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(server.PAIR_TRIES - 1):
        assert api.remote.post("/api/pair", json={"code": wrong}).status_code == 403
    assert server.pair_code() == code  # four wrong: same code
    r = api.remote.post("/api/pair", json={"code": wrong})
    assert r.status_code == 403
    new = server.pair_code()
    assert new != code and re.fullmatch(r"\d{6}", new)
    # paused: even the new, right code is refused for now
    r = api.remote.post("/api/pair", json={"code": new})
    assert r.status_code == 429 and "paused" in r.json()["error"]
    assert any("5 wrong pairing codes" in e["text"] for e in db.events())
    assert db.devices() == []

    server._pair["paused_until"] = time.time() - 1  # a minute later
    assert api.remote.post("/api/pair", json={"code": code}).status_code == 403  # the old code is gone
    assert api.remote.post("/api/pair", json={"code": new}).status_code == 200


def test_pair_success_resets_the_count(api):
    wrong = lambda: f"{(int(server.pair_code()) + 1) % 10**6:06d}"  # noqa: E731
    for _ in range(server.PAIR_TRIES - 1):
        api.remote.post("/api/pair", json={"code": wrong()})
    assert api.remote.post("/api/pair", json={"code": server.pair_code()}).status_code == 200
    for _ in range(server.PAIR_TRIES - 1):
        assert api.remote.post("/api/pair", json={"code": wrong()}).status_code == 403
    assert api.remote.post("/api/pair", json={"code": server.pair_code()}).status_code == 200


def test_pair_bad_bodies(api):
    assert api.remote.post("/api/pair", json={}).status_code == 403
    assert api.remote.post("/api/pair", json={"code": None}).status_code == 403
    assert api.remote.post("/api/pair", json={"code": int(server.pair_code())}).status_code in (200, 403)  # a number: no crash


# ---------- config ----------
def test_get_config_only_shows_editable_keys(api):
    c = api.owner.get("/api/config").json()
    assert set(c) == set(config.EDITABLE)
    for sec, keys in config.EDITABLE.items():
        assert set(c[sec]) == set(keys)
    assert "comfy_dir" not in c["image"] and "comfy_python" not in c["image"]


def test_put_config(api):
    r = api.owner.put("/api/config", json={"shift": {"variation": "wild", "schedule_time": "4:5"}, "text": {"temperature": "0.8"}})
    assert r.status_code == 200
    assert r.json()["shift"]["variation"] == "wild" and r.json()["shift"]["schedule_time"] == "04:05"
    saved = tomllib.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["shift"]["variation"] == "wild" and saved["text"]["temperature"] == 0.8


@pytest.mark.parametrize("changes", [
    {"image": {"comfy_dir": "C:/somewhere/else"}},
    {"image": {"comfy_python": "C:/evil/python.exe"}},
    {"image": {"steps": 6, "comfy_python": "C:/evil/python.exe"}},
    {"server": {"host": "0.0.0.0"}},
    {"shift": {"variation": "chaotic"}},
    {"text": {"n_ctx": "lots"}},
])
def test_put_config_rejects(api, changes):
    before = config.CONFIG_PATH.read_text(encoding="utf-8")
    r = api.owner.put("/api/config", json=changes)
    assert r.status_code == 400 and r.json()["error"]
    after = config.CONFIG_PATH.read_text(encoding="utf-8")
    assert "evil" not in after and "somewhere" not in after
    if "steps" not in str(changes):
        assert after == before


# ---------- residents ----------
def create(api, handle: str = "@Moss.Finch ", n: int = 5, **kw):
    files = [("images", (f"{i}.png", png((i * 40, 90, 200)), "image/png")) for i in range(n)]
    return api.owner.post("/api/residents", data={"name": " Moss Finch ", "handle": handle, "bio": " by the sea ", **kw}, files=files)


def test_create_resident_and_read(api):
    r = create(api)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "mossfinch" and body["handle"] == "Moss.Finch" and body["name"] == "Moss Finch" and body["bio"] == "by the sea"
    assert len(body["seeds"]) == 5 and body["status"] == "reading"
    wait_idle(api.engine)
    res = api.owner.get("/api/residents").json()[0]
    assert res["status"] == "ready" and res["moods"] == ["hushed", "sun-warmed", "domestic"]
    assert all(s["prompt"] for s in res["seeds"]) and all(s["color"].startswith("#") for s in res["seeds"])
    assert len(list(config.IMAGES.glob("*_t.jpg"))) == 5


def test_create_resident_refusals(api):
    assert create(api).status_code == 200
    assert create(api, handle="mossfinch").status_code == 400  # taken
    assert create(api, handle="other", n=9).status_code == 400  # too many images
    assert create(api, handle="none", n=0).status_code in (400, 422)
    wait_idle(api.engine)
    assert [r["id"] for r in db.residents()] == ["mossfinch"]


def test_a_bad_image_leaves_nothing_and_the_upload_can_be_tried_again(api):
    """The audit's case: five images, the last of them not an image at all."""
    files = [("images", (f"{i}.png", png() if i < 4 else b"not an image", "image/png")) for i in range(5)]
    r = api.owner.post("/api/residents", data={"name": "Bad", "handle": "bad.upload"}, files=files)
    assert r.status_code == 400 and "4.png" in r.json()["error"]
    assert db.resident("badupload") is None and db.posts() == [] and list(config.IMAGES.iterdir()) == []
    assert not api.engine.queued_read("badupload")
    assert create(api, handle="bad.upload").status_code == 200


def test_an_upload_is_turned_away_before_any_of_it_is_read(api, monkeypatch):
    """The audit's case: FastAPI parses a form, spooling its files to disk, before the route
    runs, so who's calling is asked first, in BodyLimit."""
    from starlette.datastructures import UploadFile
    written, real = [], UploadFile.write

    async def write(self, data):
        written.append(len(data))
        await real(self, data)

    monkeypatch.setattr(UploadFile, "write", write)
    files = [("images", ("big.png", b"x" * (2 * 2**20), "image/png"))]
    r = api.remote.post("/api/residents", data={"name": "x", "handle": "x"}, files=files)
    assert r.status_code == 401 and r.headers["connection"] == "close"
    assert api.remote.post("/api/residents", data={"name": "x", "handle": "x"}, files=files, headers=tok(pair(api))).status_code == 403
    assert written == [] and db.resident("x") is None
    assert create(api, handle="x").status_code == 200 and written  # the owner's upload is read as before


def test_bodies_have_a_limit(api, monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY", 1000)
    monkeypatch.setattr(server, "MAX_UPLOAD", 4000)
    assert api.owner.post("/api/dock", json={"have": [1, 2, 3]}).status_code == 200
    r = api.owner.post("/api/dock", json={"have": list(range(1000))})  # its Content-Length says so: turned away unread
    assert r.status_code == 413 and r.headers["connection"] == "close"

    def chunks():  # no Content-Length at all: counted as it comes
        yield b'{"have": ['
        for _ in range(600):
            yield b"1,"
        yield b"1]}"

    r = api.owner.post("/api/dock", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and "at most" in r.json()["error"]
    files = [("images", ("a.png", b"x" * 5000, "image/png"))]
    assert api.owner.post("/api/residents", data={"name": "x", "handle": "x"}, files=files).status_code == 413
    assert db.resident("x") is None


def test_a_render_never_lands_on_a_resident_made_again_under_the_same_handle(api, monkeypatch):
    """The audit's case: @moss is deleted and made again while an edit of the old @moss renders."""
    add_resident("moss", n_seeds=1)
    old_seed = db.seeds("moss")[0]["id"]
    db.run("UPDATE posts SET created_at = created_at - 60")
    config.update({"shift": {"posts_per_resident": 1}})
    real = api.comfy.edit

    def edit_then_replace(*args):
        out = real(*args)
        assert api.owner.delete("/api/residents/moss").status_code == 200
        assert create(api, handle="moss").status_code == 200
        return out

    monkeypatch.setattr(api.comfy, "edit", edit_then_replace)
    assert api.owner.post("/api/shift").json() == {"ok": True}
    wait_idle(api.engine)
    assert db.post(old_seed) is None and len(db.seeds("moss")) == 5
    assert db.posts("resident_id='moss' AND kind='edit'") == []
    assert db.shifts()[0]["n_posts"] == 0
    assert len(list(config.IMAGES.glob("*.png"))) == 5  # the new seeds: the render's files went with the old @moss


def test_patch_resident(api):
    add_resident("moss")
    r = api.owner.patch("/api/residents/moss", json={"moods": "Calm, Salt-stained, calm", "bio": " new bio ", "status": "failed", "id": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["moods"] == ["calm", "salt-stained"] and body["bio"] == "new bio"
    assert body["status"] == "ready" and body["id"] == "moss"  # not editable here
    assert api.owner.patch("/api/residents/nobody", json={"bio": "x"}).status_code == 404


def test_patch_resident_name_and_handle(api):
    add_resident("moss", n_seeds=1)
    add_resident("reed", n_seeds=1)
    r = api.owner.patch("/api/residents/moss", json={"name": " Moss Ashby ", "handle": " @Moss.Ashby "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "moss" and body["name"] == "Moss Ashby" and body["handle"] == "Moss.Ashby" and len(body["seeds"]) == 1
    assert db.events()[0]["text"] == "@moss is @Moss.Ashby now."
    assert api.owner.patch("/api/residents/moss", json={"handle": "moss_ashby"}).json()["handle"] == "moss_ashby"  # its own, written another way
    for change in ({"handle": "Reed"}, {"handle": "@"}, {"name": "  "}, {"name": None}, {"bio": 5}):
        assert api.owner.patch("/api/residents/moss", json=change).status_code == 400, change
    assert db.resident("moss")["handle"] == "moss_ashby" and db.resident("moss")["name"] == "Moss Ashby"


def test_a_handle_given_up_can_be_used_again(api):
    add_resident("moss", n_seeds=1)
    assert api.owner.patch("/api/residents/moss", json={"handle": "wren"}).status_code == 200
    assert create(api, handle="wren").status_code == 400  # taken now
    r = create(api, handle="moss")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "moss2" and r.json()["handle"] == "moss"  # the id "moss" is still the first resident's
    wait_idle(api.engine)
    assert {r["id"]: r["handle"] for r in db.residents()} == {"moss": "wren", "moss2": "moss"}


def test_delete_resident_removes_its_images(api):
    add_resident("moss", n_seeds=2)
    add_edit("moss", db.seeds("moss")[0]["id"])
    keep = add_resident("reed", n_seeds=1)
    assert len(list(config.IMAGES.glob("*.png"))) == 4
    assert api.owner.delete("/api/residents/moss").json() == {"ok": True}
    assert len(list(config.IMAGES.glob("*.png"))) == 1 and db.resident("moss") is None and keep


def test_read_again_409_while_queued_then_allowed_after_a_failure(api, monkeypatch):
    add_resident("moss", status="ready", n_seeds=1)
    entered, release, fail = threading.Event(), threading.Event(), {"now": True}
    real = Engine._read_seeds

    def read_seeds(self, res, seeds):
        entered.set()
        assert release.wait(10)
        if fail["now"]:
            raise RuntimeError("the model file is missing")
        return real(self, res, seeds)

    monkeypatch.setattr(Engine, "_read_seeds", read_seeds)
    try:
        r = api.owner.post("/api/residents/moss/read")
        assert r.status_code == 200 and r.json()["status"] == "reading"
        assert entered.wait(5)
        r = api.owner.post("/api/residents/moss/read")
        assert r.status_code == 409 and "already queued or running" in r.json()["error"]
    finally:
        release.set()
    wait_idle(api.engine)
    assert db.resident("moss")["status"] == "failed"
    assert api.owner.get("/api/status").json()["engine"]["state"] == "error"

    fail["now"] = False
    r = api.owner.post("/api/residents/moss/read")  # "Read again"
    assert r.status_code == 200
    wait_idle(api.engine)
    assert db.resident("moss")["status"] == "ready"
    assert api.owner.post("/api/residents/nobody/read").status_code == 404


def test_memory_and_graph(api):
    add_resident("moss", n_seeds=1)
    seed = db.seeds("moss")[0]["id"]
    bite = add_edit("moss", seed, vote=1, prompt="a bitten prompt")
    throw = add_edit("moss", seed, vote=-1, prompt="a thrown prompt")
    snag = add_edit("moss", seed, vote=1, snagged=True, prompt="a snagged prompt")
    m = api.owner.get("/api/residents/moss/memory").json()
    assert m["good"] == [bite] and m["bad"] == [throw]
    assert "a bitten prompt" in m["system"] and "a snagged prompt" not in m["system"]
    assert [p["id"] for p in m["bites"]] == [bite] and [p["id"] for p in m["throws"]] == [throw]
    graph = api.owner.get("/api/residents/moss/graph").json()
    assert [n["id"] for n in graph] == [seed, bite, throw] and snag not in [n["id"] for n in graph]
    assert all(n["color"].startswith("#") for n in graph)
    assert api.owner.get("/api/residents/nobody/memory").status_code == 404


# ---------- posts, votes, snags ----------
@pytest.fixture
def feed(api):
    add_resident("moss", n_seeds=1)
    seed = db.seeds("moss")[0]["id"]
    return SimpleNamespace(seed=seed, e1=add_edit("moss", seed), e2=add_edit("moss", seed, vote=1),
                           old=add_edit("moss", seed, created_at=time.time() - 10 * 86400))


def test_posts_and_filters(api, feed):
    ids = lambda r: [p["id"] for p in r.json()]  # noqa: E731
    assert ids(api.owner.get("/api/posts")) == [feed.old, feed.e2, feed.e1, feed.seed]
    assert ids(api.owner.get("/api/posts?kind=edit")) == [feed.old, feed.e2, feed.e1]
    assert ids(api.owner.get("/api/posts?days=7")) == [feed.e2, feed.e1, feed.seed]  # seeds whatever their age
    assert ids(api.owner.get("/api/posts?resident=nobody")) == []
    assert ids(api.owner.get("/api/posts?limit=1")) == [feed.old]
    p = api.owner.get(f"/api/posts/{feed.e2}").json()
    assert p["vote"] == 1 and p["snagged"] is False and [c["id"] for c in p["chain"]] == [feed.seed]
    assert api.owner.get("/api/posts/99999").status_code == 404


def test_votes_newer_wins(api, feed):
    token = pair(api)
    now = time.time()
    api.owner.post("/api/votes", json={"votes": [{"id": feed.e1, "vote": 1}]})
    r = api.remote.post("/api/votes", json={"votes": [{"id": feed.e1, "vote": -1, "at": now - 3600}]}, headers=tok(token))
    assert r.json() == {"ok": True}
    assert db.post(feed.e1)["vote"] == 1  # the phone's vote was older
    api.remote.post("/api/votes", json={"votes": [{"id": feed.e1, "vote": -1, "at": time.time()}]}, headers=tok(token))
    p = db.post(feed.e1)
    assert p["vote"] == -1 and p["voted_by"] == "phone"


def test_snag_and_unsnag(api, feed):
    r = api.owner.post(f"/api/posts/{feed.e1}/snag", json={"snag": True})
    assert r.status_code == 200 and r.json()["snagged"] is True
    assert feed.e1 not in [p["id"] for p in api.owner.get("/api/posts").json()]
    assert [p["id"] for p in api.owner.get("/api/posts?snagged=true").json()] == [feed.e1]
    counts = api.owner.get("/api/status").json()["counts"]
    assert counts["snags"] == 1 and counts["posts"] == 2
    r = api.owner.post(f"/api/posts/{feed.e1}/snag", json={"snag": False})
    assert r.status_code == 200 and r.json()["snagged"] is False
    assert feed.e1 in [p["id"] for p in api.owner.get("/api/posts").json()]
    # undoing a post that was never snagged is fine
    assert api.owner.post(f"/api/posts/{feed.e2}/snag", json={"snag": False}).status_code == 200


def test_unsnag_409_after_the_purge(api, feed):
    api.owner.post(f"/api/posts/{feed.e1}/snag", json={})  # {} means snag
    stems = db.purge_snags()  # what the next night shift does first
    assert len(stems) == 1
    r = api.owner.post(f"/api/posts/{feed.e1}/snag", json={"snag": False})
    assert r.status_code == 409 and "too late" in r.json()["error"]
    assert api.owner.get("/api/status").json()["counts"]["snags"] == 1


def test_snag_only_edits(api, feed):
    assert api.owner.post(f"/api/posts/{feed.seed}/snag", json={"snag": True}).status_code == 404
    assert api.owner.post("/api/posts/99999/snag", json={"snag": True}).status_code == 404


def test_a_device_can_snag(api, feed):
    token = pair(api)
    assert api.remote.post(f"/api/posts/{feed.e1}/snag", json={"snag": True}, headers=tok(token)).json()["snagged"] is True


# ---------- the dock ----------
def test_dock(api):
    now = time.time()
    add_resident("moss", n_seeds=2)
    add_resident("reed", status="reading", n_seeds=1)  # being read again: its posts still go out, so it must too
    add_resident("ghost", status="failed", n_seeds=0)  # nothing of it goes out
    seed = db.seeds("moss")[0]["id"]
    reed_seed = db.seeds("reed")[0]["id"]
    had = SimpleNamespace(
        old=add_edit("moss", seed, created_at=now - 8 * 86400),  # older than keep_nights
        thrown=add_edit("moss", seed, vote=-1),  # thrown back on the PC since the last dock
        snagged=add_edit("moss", seed, snagged=True),  # snagged on the PC
        bitten=add_edit("moss", seed),  # bitten on the PC below
        to_vote=add_edit("moss", seed),
        to_snag=add_edit("reed", reed_seed),
    )
    fresh = add_edit("moss", seed)  # new since the last dock
    old_seed_era = add_edit("moss", seed, created_at=now - 30 * 86400, vote=1)  # old, never sent: stays home
    api.owner.post("/api/votes", json={"votes": [{"id": had.bitten, "vote": 1}]})
    token = pair(api, name="phone")
    have = [seed, db.seeds("moss")[1]["id"], reed_seed, *vars(had).values(), 99999]
    body = {
        "votes": [
            {"id": had.to_vote, "vote": 1, "at": now},
            {"id": had.to_snag, "snag": True, "at": now},
            {"id": fresh, "vote": -1, "snag": True, "at": now},  # a vote and a snag at once
            {"id": had.bitten, "vote": -1, "at": now - 7200},  # older than the PC's bite: loses
        ],
        "have": have,
    }
    r = api.remote.post("/api/dock", json=body, headers=tok(token))
    assert r.status_code == 200, r.text
    d = r.json()

    # up: votes and snags came home
    assert db.post(had.to_vote)["vote"] == 1 and db.post(had.to_vote)["voted_by"] == "phone"
    assert db.post(had.to_snag)["snagged_at"] is not None
    assert db.post(fresh)["vote"] == -1 and db.post(fresh)["snagged_at"] is not None
    assert db.post(had.bitten)["vote"] == 1  # the owner's newer bite stands

    # down: nothing new (fresh was thrown back and snagged on the way up), and what to drop
    assert [p["id"] for p in d["posts"]] == []
    assert d["drop"] == sorted([had.old, had.thrown, had.snagged, had.to_snag, 99999])
    assert d["votes"] == {str(had.bitten): 1, str(had.to_vote): 1, str(seed): 0, str(db.seeds("moss")[1]["id"]): 0, str(reed_seed): 0}
    assert {r["id"] for r in d["residents"]} == {"moss", "reed"}
    assert all(r["seeds"] is None for r in d["residents"])
    assert d["keep_nights"] == 7 and abs(d["now"] - time.time()) < 60
    assert old_seed_era not in [p["id"] for p in d["posts"]]

    dev = db.device(token)
    assert dev["last_dock_at"] is not None
    assert db.q("SELECT up, down FROM docks") == [{"up": 4, "down": 0}]
    assert "phone docked: 3 votes, 2 snags home, 0 posts out, 5 dropped." in [e["text"] for e in db.events()]


def test_dock_keeps_a_votes_own_time_apart_from_its_snags(api):
    """The audit's case: on the phone, a bite at 100 and a snag at 200; on the PC, a throw-back at 150.
    The PC's vote is the newer one, so it stands; an older phone app's single `at` still works (test_dock)."""
    add_resident("moss", n_seeds=1)
    pid = add_edit("moss", db.seeds("moss")[0]["id"])
    now = time.time()
    db.vote(pid, -1, "pc", now - 50)
    token = pair(api)
    body = {"votes": [{"id": pid, "vote": 1, "voted_at": now - 100, "snag": True, "unsnag": False, "snagged_at": now}], "have": [pid]}
    assert api.remote.post("/api/dock", json=body, headers=tok(token)).status_code == 200
    p = db.post(pid)
    assert p["vote"] == -1 and p["voted_by"] == "pc" and p["snagged_at"] == pytest.approx(now)


def test_dock_takes_an_undone_snag_home(api):
    """A snag the phone undid after a dock took it out (that dock may not have come back)."""
    add_resident("moss", n_seeds=1)
    seed = db.seeds("moss")[0]["id"]
    pid = add_edit("moss", seed)
    token = pair(api, name="phone")
    now = time.time()
    snag = {"id": pid, "vote": None, "voted_at": now - 20, "snag": True, "unsnag": False, "snagged_at": now - 10}
    d = api.remote.post("/api/dock", json={"votes": [snag], "have": [seed, pid]}, headers=tok(token)).json()
    assert db.post(pid)["snagged_at"] is not None and d["drop"] == [pid]
    undo = {**snag, "snag": False, "unsnag": True, "snagged_at": now}
    d = api.remote.post("/api/dock", json={"votes": [undo], "have": [seed, pid]}, headers=tok(token)).json()
    assert db.post(pid)["snagged_at"] is None and d["drop"] == [] and d["votes"][str(pid)] == 0
    assert "phone docked: 0 votes, 1 snag undone home, 0 posts out, 0 dropped." in [e["text"] for e in db.events()]
    # an undo older than the snag on the Angler (the PC snagged it again since) loses
    api.owner.post(f"/api/posts/{pid}/snag", json={"snag": True})
    api.remote.post("/api/dock", json={"votes": [{**undo, "snagged_at": now - 5}], "have": [pid]}, headers=tok(token))
    assert db.post(pid)["snagged_at"] is not None


def test_dock_first_time_gets_everything_live(api):
    now = time.time()
    add_resident("moss", n_seeds=1)
    add_resident("reed", status="reading", n_seeds=1)
    seed = db.seeds("moss")[0]["id"]
    e1, e2 = add_edit("moss", seed, vote=1), add_edit("moss", seed)
    add_edit("moss", seed, vote=-1)
    add_edit("moss", seed, snagged=True)
    add_edit("moss", seed, created_at=now - 8 * 86400)
    token = pair(api)
    d = api.remote.post("/api/dock", json={}, headers=tok(token)).json()
    sent = {p["id"] for p in d["posts"]}
    assert sent == {seed, db.seeds("reed")[0]["id"], e1, e2}
    assert {r["id"] for r in d["residents"]} == {"moss", "reed"}  # a resident for every post sent
    assert {p["resident_id"] for p in d["posts"]} <= {r["id"] for r in d["residents"]}
    assert d["drop"] == [] and d["votes"] == {}
    by_id = {p["id"]: p for p in d["posts"]}
    assert by_id[e1]["vote"] == 1 and set(by_id[e1]) >= {"id", "resident_id", "kind", "source_id", "prompt", "caption", "tags", "image",
                                                         "color", "snagged", "created_at", "vote"}


def test_dock_keep_nights(api):
    add_resident("moss", n_seeds=1)
    seed = db.seeds("moss")[0]["id"]
    three_days = add_edit("moss", seed, created_at=time.time() - 3 * 86400)
    config.update({"device": {"keep_nights": 2}})
    d = api.owner.post("/api/dock", json={"have": [three_days]}).json()  # this machine docks too, untracked
    assert d["drop"] == [three_days] and d["keep_nights"] == 2
    assert db.q("SELECT COUNT(*) AS n FROM docks") == [{"n": 0}]


# ---------- the night shift through the API ----------
def test_shift_through_the_api(api):
    add_resident("moss", n_seeds=2)
    db.run("UPDATE posts SET created_at = created_at - 60")
    config.update({"shift": {"posts_per_resident": 2}})
    assert api.owner.post("/api/shift").json() == {"ok": True}
    wait_idle(api.engine)
    shifts = api.owner.get("/api/shifts").json()
    assert shifts[0]["status"] == "done" and shifts[0]["n_posts"] == 2 and shifts[0]["n_snags"] == 0
    edits = api.owner.get("/api/posts?kind=edit").json()
    assert len(edits) == 2 and all(p["source_id"] for p in edits)
    assert any("night shift #1" in e["text"] for e in api.owner.get("/api/journal").json())


def test_second_shift_refused_while_one_is_queued(api, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def run_shift(self, sid, trigger):
        entered.set()
        assert release.wait(10)
        return 0

    monkeypatch.setattr(Engine, "_run_shift", run_shift)
    try:
        assert api.owner.post("/api/shift").json() == {"ok": True}
        assert entered.wait(5)
        r = api.owner.post("/api/shift").json()
        assert r["ok"] is False and "already" in r["reason"]
        assert api.owner.get("/api/status").json()["engine"]["job"] == "shift"
    finally:
        release.set()


# ---------- media and the web apps ----------
def test_media(api):
    stem = media.save(png())
    r = api.owner.get(f"/media/{stem}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"]
    assert api.owner.get(f"/media/{stem}_t.jpg").status_code == 200
    assert api.remote.get(f"/media/{stem}.jpg").status_code == 401
    token = pair(api)
    assert api.remote.get(f"/media/{stem}.jpg?t={token}").status_code == 200
    assert api.owner.get("/media/nope.jpg").status_code == 404


@pytest.mark.parametrize("name", ["..%5Cbaitbook.toml", "..%2Fbaitbook.toml", "%2E%2E%5Cdata%5Cbaitbook.db", "..%5C..%5Chome%5Cbaitbook.toml"])
def test_media_refuses_traversal(api, name):
    r = api.owner.get(f"/media/{name}")
    assert r.status_code == 404
    assert b"[server]" not in r.content


def test_service_worker(api):
    r = api.remote.get("/device/sw.js")  # no token needed: it's the app shell
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    m = re.search(r"const VERSION = '([^']*)';", r.text)
    assert m and re.fullmatch(r"[0-9a-f]{12}", m.group(1))
    assert "const VERSION = 'dev';" not in r.text
    assert api.remote.get("/device/sw.js").text == r.text  # stable


def test_service_worker_version_follows_the_shell(api, tmp_path, monkeypatch):
    web = tmp_path / "web"
    shutil.copytree(ROOT / "web", web)
    monkeypatch.setattr(server, "WEB", web)
    v1 = server.shell_version()
    (web / "app" / "app.css").write_text("/* the PC app isn't in the phone's shell */", encoding="utf-8")
    assert server.shell_version() == v1
    (web / "shared" / "theme.css").write_text("body { color: red }", encoding="utf-8")
    v2 = server.shell_version()
    assert v2 != v1
    assert f"const VERSION = '{v2}';" in api.owner.get("/device/sw.js").text


def sw_files() -> list[str]:
    js = (ROOT / "web" / "device" / "sw.js").read_text(encoding="utf-8")
    start = js.index("const FILES")
    return re.findall(r"'([^']+)'", js[start:js.index("];", start)])


def theme_refs() -> list[str]:
    """The files theme.css loads with url(), as paths the Angler serves (data: URIs left out)."""
    css = (ROOT / "web" / "shared" / "theme.css").read_text(encoding="utf-8")
    urls = (u.strip("'\"") for u in re.findall(r"url\(([^)]+)\)", css))
    return ["/shared/" + u for u in urls if not u.startswith("data:")]


def test_service_worker_files_all_exist(api):
    """A file the service worker caches but the Angler can't serve fails the phone's install."""
    files = sw_files()
    assert len(files) >= 10
    for f in files:
        url = f if f.startswith("/") else f"/device/{f}"
        assert api.remote.get(url).status_code == 200, url


def test_service_worker_caches_what_the_theme_loads():
    """A file theme.css loads (the hook mark) but the shell cache lacks is missing from the phone offline."""
    refs = theme_refs()
    assert "/shared/mark.svg" in refs
    assert set(refs) <= set(sw_files())


@pytest.mark.parametrize("folder", ["app", "device"])
def test_web_references_resolve(api, folder):
    """Every local href / src in the app's index.html (and the phone's manifest icons, the fonts' and the theme's url()s) is served."""
    html = (ROOT / "web" / folder / "index.html").read_text(encoding="utf-8")
    refs = [r for r in re.findall(r'(?:href|src)="([^"]+)"', html) if not re.match(r"(?:[a-z]+:|#|//)", r)]
    if folder == "device":
        import json

        manifest = json.loads((ROOT / "web" / "device" / "manifest.webmanifest").read_text(encoding="utf-8"))
        refs += [icon["src"] for icon in manifest.get("icons", [])]
    fonts_css = (ROOT / "web" / "shared" / "fonts" / "fonts.css").read_text(encoding="utf-8")
    refs += ["/shared/fonts/" + u.strip("'\"") for u in re.findall(r"url\(([^)]+)\)", fonts_css)]
    refs += theme_refs()
    assert refs
    for ref in refs:
        url = ref if ref.startswith("/") else f"/{folder}/{ref}"
        assert api.remote.get(url).status_code == 200, url


def test_fonts_are_woff2(api):
    fonts = sorted((ROOT / "web" / "shared" / "fonts").glob("*.woff2"))
    assert fonts
    for f in fonts:
        r = api.remote.get(f"/shared/fonts/{f.name}")
        assert r.status_code == 200 and r.headers["content-type"] == "font/woff2", f.name


@pytest.mark.parametrize("path", ["/app/", "/device/", "/app/index.html", "/device/index.html", "/device/manifest.webmanifest",
                                  "/shared/api.js", "/shared/theme.css"])
def test_web_files(api, path):
    r = api.remote.get(path)
    assert r.status_code == 200 and r.content


def test_root_redirects_to_the_app(api):
    r = api.owner.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/app/"
    html = api.owner.get("/").text
    assert html.lower().lstrip().startswith("<!doctype html>")


# ---------- shutdown (`baitbook stop`) ----------
def test_shutdown_calls_stop(api, monkeypatch):
    stopped = threading.Event()
    monkeypatch.setattr(server.app.state, "stop", stopped.set, raising=False)
    r = api.owner.post("/api/shutdown")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert stopped.wait(5)


def test_shutdown_without_a_way_to_stop(api, monkeypatch):
    monkeypatch.delattr(server.app.state, "stop", raising=False)
    assert api.owner.post("/api/shutdown").status_code == 503


def test_shutdown_is_local_only(api, monkeypatch):
    stopped = threading.Event()
    monkeypatch.setattr(server.app.state, "stop", stopped.set, raising=False)
    owner_token = pair(api, role="owner")
    assert api.remote.post("/api/shutdown").status_code == 403
    assert api.remote.post("/api/shutdown", headers=tok(owner_token)).status_code == 403  # not even a paired owner
    proxied = TestClient(server.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50004), headers={"X-Forwarded-For": "1.2.3.4"})
    assert proxied.post("/api/shutdown").status_code == 403
    time.sleep(0.5)  # the timer would have fired by now
    assert not stopped.is_set()


# ---------- the whole loop: create, read, shift, dock ----------
def test_end_to_end(api):
    assert create(api, handle="moss").status_code == 200
    wait_idle(api.engine)
    db.run("UPDATE posts SET created_at = created_at - 60")
    config.update({"shift": {"posts_per_resident": 3}})
    api.owner.post("/api/shift")
    wait_idle(api.engine)
    edits = api.owner.get("/api/posts?kind=edit").json()
    assert len(edits) == 3

    token = pair(api)
    d = api.remote.post("/api/dock", json={"have": []}, headers=tok(token)).json()
    assert len(d["posts"]) == 8 and [r["id"] for r in d["residents"]] == ["moss"]
    have = [p["id"] for p in d["posts"]]
    snag, bite = edits[0]["id"], edits[1]["id"]
    d2 = api.remote.post("/api/dock", headers=tok(token),
                         json={"have": have, "votes": [{"id": bite, "vote": 1, "at": time.time()}, {"id": snag, "snag": True, "at": time.time()}]}).json()
    assert d2["posts"] == [] and d2["drop"] == [snag] and d2["votes"][str(bite)] == 1
    s = api.owner.get("/api/status").json()["counts"]
    assert (s["posts"], s["votes"], s["bites"], s["snags"]) == (2, 1, 1, 1)
    res = api.owner.get("/api/residents").json()[0]
    assert (res["n_posts"], res["n_bites"], res["n_snags"]) == (2, 1, 1)
    wait_for(lambda: api.engine.snapshot()["state"] == "idle", what="idle engine")


def test_phone_addresses_follow_the_port_the_angler_runs_on(home, monkeypatch):
    import socket

    monkeypatch.setattr(server.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("192.168.1.20", 0))])
    monkeypatch.setattr(server, "route_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(server, "_ips", (0.0, []))
    monkeypatch.setattr(server, "_serve", (0.0, None))
    asked = []
    monkeypatch.setattr(server.tailscale, "serve_url", lambda port: asked.append(port))
    config.override({"server": {"port": 9000}})  # `baitbook angler --port 9000`
    assert server.lan_urls() == ["http://192.168.1.20:9000/device/"]
    server.tailscale_url()
    assert asked == [9000]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_no_lan_addresses_when_only_this_machine_listens(home, monkeypatch, host):
    """[server] host = "127.0.0.1" (every phone comes through Tailscale Serve): no Wi-Fi address reaches it."""
    monkeypatch.setattr(server, "_ips", (0.0, []))
    config.override({"server": {"host": host}})
    assert server.lan_urls() == []
