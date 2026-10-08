"""The Angler's HTTP API, plus the two clients it serves.

  /app/      the PC app (owner console + feed). Also what pcapp/ opens in a window.
  /device/   the phone app (PWA): offline feed that docks to sync.
  /media/    images (JPEG variants; PNG masters stay home).

Requests from this machine are the owner's (but not a web page from elsewhere
that this machine's browser is showing). Anything else needs a device token,
got once by pairing with the 6-digit code the PC app shows: a phone pairs as a
device (read, vote, dock), a PC app on another machine pairs as the owner.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import mimetypes
import random
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from . import config, db, media, tailscale
from .engine import Engine, slug
from .imagegen import stop_started_comfy

WEB = config.WEB
HUES = ["#E8C9A8", "#BFD9D8", "#C7B8DA", "#F1DC9C", "#C5D3B4", "#EBC4B5"]
LOCAL = {"127.0.0.1", "::1", "localhost"}
# a reverse proxy on this machine connects from 127.0.0.1 too; these give it away
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "tailscale-user-login")
PAIR_TRIES, PAIR_PAUSE_S = 5, 60
MAX_BODY = 8 * 2**20  # any request: a dock's list of every post on the phone fits many times over
MAX_UPLOAD = 256 * 2**20  # a new resident's seed images (up to 8 photos)
mimetypes.add_type("font/woff2", ".woff2")  # web/shared/fonts/; Python on Windows doesn't know it

app = FastAPI(title="baitbook · Angler")
engine: Engine | None = None
CODE = config.code_version()  # the code this Angler runs: what was on disk as it started
log = logging.getLogger("angler.server")
_pair = {"code": "", "fails": 0, "paused_until": 0.0}
_pair_lock = threading.Lock()


def _new_code() -> None:
    _pair.update(code=f"{secrets.randbelow(10**6):06d}", fails=0)


def pair_code() -> str:
    with _pair_lock:
        if not _pair["code"]:
            _new_code()
        return _pair["code"]


@app.on_event("startup")
def _start() -> None:
    global engine
    config.load()
    db.conn()
    engine = Engine()


@app.on_event("shutdown")
def _stop() -> None:
    """A ComfyUI the Angler started goes with it, so it doesn't sit on the GPU."""
    stop_started_comfy()


# ---------- auth ----------
def is_local(request: Request) -> bool:
    """This machine, asked directly: a loopback peer, a loopback Host, no proxy headers, and not
    a web page from another origin (same_origin)."""
    peer = request.client.host if request.client else ""
    if peer not in LOCAL or any(h in request.headers for h in PROXY_HEADERS) or not same_origin(request):
        return False
    host = (request.headers.get("host") or "").lower()
    host = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.split(":")[0]
    return host in LOCAL


def same_origin(request: Request) -> bool:
    """Not a web page from somewhere else. Any page open in a browser on this machine, even one
    served from another port here, can send the Angler a form or a simple fetch (the browser only
    keeps it from reading the answer). So a browser's request counts only from the Angler's own
    pages: Sec-Fetch-Site same-origin (or none, an address typed in), and an Origin, if there is
    one, naming this very Host. The CLI sends neither header."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc.lower() == (request.headers.get("host") or "").lower()


def client(request: Request) -> dict:
    """Who's calling: this machine, or a paired device. Everyone else is refused."""
    if is_local(request):
        return {"name": "pc", "token": None, "local": True, "owner": True}
    token = request.headers.get("x-baitbook-token") or request.query_params.get("t")
    dev = db.device(token) if token else None
    if not dev:
        raise HTTPException(401, "pair this device first")
    return {"name": dev["name"], "token": token, "local": False, "owner": dev.get("role") == "owner"}


def owner(request: Request) -> dict:
    """The owner: this machine, or a PC app paired as the owner. Phones get a 403."""
    who = client(request)
    if not who["owner"]:
        raise HTTPException(403, "only the owner can do this: from the Angler's machine, or a PC app paired as the owner")
    return who


def too_big(limit: int) -> HTTPException:
    return HTTPException(413, f"too much at once: at most {limit // 2**20} MB", headers={"Connection": "close"})


class BodyLimit:
    """Ahead of every route: FastAPI reads a request's body, and spools a form's files to disk,
    before the route asks who's calling. So the seed-image upload is the owner's alone from here,
    before any of it is read, and no body may run past its limit, whatever Content-Length says."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_BODY
        try:
            if scope["method"] == "POST" and scope["path"] == "/api/residents":
                owner(Request(scope))
                limit = MAX_UPLOAD
            size = Headers(scope=scope).get("content-length", "")
            if size.isdigit() and int(size) > limit:
                raise too_big(limit)
        except HTTPException as exc:  # answered without reading the body, and the connection closed
            response = JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers={"Connection": "close"})
            return await response(scope, receive, send)
        seen = 0

        async def capped() -> dict:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > limit:
                raise too_big(limit)  # in the route's body parsing, which hands it on as the answer
            return message

        await self.app(scope, capped, send)


app.add_middleware(BodyLimit)


def post_json(p: dict) -> dict:
    out = {k: p[k] for k in ("id", "resident_id", "kind", "source_id", "prompt", "caption", "tags", "image", "shift_id", "created_at", "vote")}
    out["color"] = media.avg_color(p["image"]) if p["image"] else "#c9a27a"
    out["snagged"] = bool(p.get("snagged_at"))
    return out


LIVE = "snagged_at IS NULL"  # every feed, count and graph leaves snags out


# ---------- status & config ----------
@app.get("/api/status")
def status(request: Request):
    who = client(request)
    edits = db.posts(f"kind='edit' AND {LIVE}", limit=100000)
    voted = [p for p in edits if p["vote"]]
    return {
        "engine": engine.snapshot(),
        # snags count even after their images are cleared: the snag rate is snags / every edit made
        "counts": {"residents": len(db.residents()), "posts": len(edits), "votes": len(voted), "bites": sum(p["vote"] > 0 for p in voted),
                   "snags": db.count_snags()},
        "models": {"text": config.section("text")["name"], "image": config.section("image")["name"]},
        "pair_code": pair_code() if who["owner"] else None,
        "devices": db.devices() if who["owner"] else None,
        "lan_urls": lan_urls() if who["owner"] else None,
        "tailscale_url": tailscale_url() if who["owner"] else None,
        "paths": {"config": str(config.CONFIG_PATH), "data": str(config.DATA)} if who["local"] else None,
        # stale: the code on disk has changed since this Angler started; `baitbook` restarts a
        # background one that's idle, and the PC app says so otherwise
        "code": CODE,
        "stale": config.code_version() != CODE,
        "background": getattr(app.state, "background", False),
        "you": who["name"],
        "owner": who["owner"],
        "now": time.time(),
    }


_ips: tuple[float, list[str]] = (0.0, [])  # (when, addresses): looked up at most once a minute
_serve: tuple[float, str | None] = (0.0, None)  # (when, address): asked at most every 30 s
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's addresses


def route_ip() -> str | None:
    """The address this machine reaches the network from: its Wi-Fi or Ethernet one, never a
    virtual adapter's (WSL, Hyper-V, Docker). connect() on UDP only picks a route; nothing is
    sent, and 192.0.2.1 (TEST-NET-1) is never anyone's."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_urls() -> list[str]:
    """Where a phone on the same network can reach this Angler: the address of the adapter this
    machine reaches the network through, or every address if that can't be told. Tailscale's are
    left out; a phone comes through Tailscale at tailscale_url(). The lookup can be slow on some
    machines, and /api/status is polled, so its answer is kept for a minute. None at all when the
    Angler only listens on this machine ([server] host or --host "127.0.0.1")."""
    global _ips
    cfg = config.section("server")
    if str(cfg.get("host") or "").strip("[]") in LOCAL:
        return []
    scheme = "https" if cfg.get("ssl_certfile") else "http"
    if time.time() - _ips[0] > 60:
        found = set()
        try:
            found.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        except OSError:
            pass
        found = {ip for ip in found if not ip.startswith(("127.", "169.254.")) and ipaddress.ip_address(ip) not in TAILNET}
        route = route_ip()
        _ips = (time.time(), [route] if route in found else sorted(found))
    return [f"{scheme}://{ip}:{cfg['port']}/device/" for ip in _ips[1]]


def tailscale_url() -> str | None:
    """The phone app's HTTPS address through `tailscale serve`, if it passes this Angler on (the
    one to use: the app works offline there). It asks the `tailscale` command, so its answer is
    kept for 30 s."""
    global _serve
    if time.time() - _serve[0] > 30:
        _serve = (time.time(), tailscale.serve_url(config.local_port(config.section("server"))))
    return _serve[1]


@app.get("/api/config")
def get_config(request: Request):
    client(request)
    c = config.get()
    return {sec: {k: c[sec].get(k) for k in keys} for sec, keys in config.EDITABLE.items()}


@app.put("/api/config")
def put_config(request: Request, changes: dict = Body(...)):
    owner(request)
    try:
        config.update(changes)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return get_config(request)


# ---------- residents ----------
def resident_json(r: dict) -> dict:
    posts = db.posts(f"resident_id=? AND {LIVE}", (r["id"],), limit=100000)
    edits = [p for p in posts if p["kind"] == "edit"]
    voted = [p for p in edits if p["vote"]]
    return {**r, "seeds": [post_json(p) for p in posts if p["kind"] == "seed"][::-1],
            "n_posts": len(edits), "n_votes": len(voted), "n_bites": sum(p["vote"] > 0 for p in voted), "n_snags": db.count_snags(r["id"])}


@app.get("/api/residents")
def list_residents(request: Request):
    client(request)
    return [resident_json(r) for r in db.residents()]


def handle_taken(handle: str, but: str = "") -> bool:
    """Is `handle` another resident's (not `but`'s)? Handles that differ only in case and
    punctuation (Moss.Finch, moss_finch) count as the same one."""
    key = slug(handle) or handle.lower()
    return any((slug(r["handle"]) or r["handle"].lower()) == key for r in db.residents() if r["id"] != but)


@app.post("/api/residents")
async def create_resident(request: Request, name: str = Form(...), handle: str = Form(...), bio: str = Form(""),
                          images: list[UploadFile] = File(...)):
    owner(request)  # BodyLimit turned everyone else away before the images came in
    handle = handle.strip().lstrip("@")
    if not name.strip() or not handle:
        raise HTTPException(400, "a resident needs a name and a handle")
    if handle_taken(handle) or not (1 <= len(images) <= 8):
        raise HTTPException(400, "that handle is taken, or the image count is off (give 5)")
    # the id is the handle's slug, unless a resident whose handle has changed since still has it
    rid = base = slug(handle)[:24] or f"r{int(time.time())}"
    n = 2
    while db.resident(rid):
        rid, n = f"{base}{n}", n + 1
    # every image is stored before the resident exists, so a bad one leaves nothing behind and the upload can be tried again
    stems: list[str] = []
    try:
        for up in images:
            try:
                stems.append(media.save(await up.read()))
            except media.NotAnImage as exc:
                raise HTTPException(400, f"{up.filename or 'one of the files'} isn't an image baitbook can read") from exc
        db.add_resident(rid, name.strip(), handle, bio.strip(), HUES[hash(rid) % len(HUES)], seeds=stems)
    except BaseException:
        for stem in stems:
            media.delete(stem)
        raise
    engine.submit("read", resident_id=rid)
    db.event("resident", f"new resident @{handle} with {len(images)} seed images; the vision read is queued.", rid)
    return resident_json(db.resident(rid))


@app.patch("/api/residents/{rid}")
def edit_resident(rid: str, request: Request, body: dict = Body(...)):
    """Name, handle, bio and mood tags. The id stays as it was, so posts and links still find the resident."""
    owner(request)
    r = db.resident(rid)
    if not r:
        raise HTTPException(404)
    fields = {k: v for k, v in body.items() if k in ("name", "handle", "bio", "moods")}
    for k in ("name", "handle", "bio"):
        if k not in fields:
            continue
        if not isinstance(fields[k], str):
            raise HTTPException(400, f"{k} has to be text")
        fields[k] = fields[k].strip().lstrip("@") if k == "handle" else fields[k].strip()
    if fields.get("name") == "" or fields.get("handle") == "":
        raise HTTPException(400, "a resident needs a name and a handle")
    if "handle" in fields and handle_taken(fields["handle"], but=rid):
        raise HTTPException(400, f"@{fields['handle']} is taken")
    if isinstance(fields.get("moods"), str):
        from .prompts import parse_tags
        fields["moods"] = parse_tags(fields["moods"])
    db.update_resident(rid, **fields)
    if fields.get("handle", r["handle"]) != r["handle"]:
        db.event("resident", f"@{r['handle']} is @{fields['handle']} now.", rid)
    return resident_json(db.resident(rid))


@app.delete("/api/residents/{rid}")
def remove_resident(rid: str, request: Request):
    owner(request)
    for stem in db.delete_resident(rid):
        media.delete(stem)
    return {"ok": True}


@app.post("/api/residents/{rid}/read")
def reread(rid: str, request: Request):
    """Run the vision read again: after one failed, or to rewrite the base prompts."""
    owner(request)
    r = db.resident(rid)
    if not r:
        raise HTTPException(404)
    if engine.queued_read(rid):
        raise HTTPException(409, "a vision read of this resident is already queued or running")
    db.update_resident(rid, status="reading")
    engine.submit("read", resident_id=rid)
    db.event("resident", f"the vision read of @{r['handle']} is queued again.", rid)
    return resident_json(db.resident(rid))


@app.get("/api/residents/{rid}/memory")
def memory(rid: str, request: Request):
    client(request)
    r = db.resident(rid)
    if not r:
        raise HTTPException(404)
    ctx = engine.context(r, random.Random())
    return {"resident": resident_json(r), "system": ctx["system"],
            "good": [p["id"] for p in ctx["good"]], "bad": [p["id"] for p in ctx["bad"]], "n_good": ctx["n_good"], "n_bad": ctx["n_bad"],
            "bites": [post_json(p) for p in db.posts(f"resident_id=? AND kind='edit' AND vote>0 AND {LIVE}", (rid,))],
            "throws": [post_json(p) for p in db.posts(f"resident_id=? AND kind='edit' AND vote<0 AND {LIVE}", (rid,))]}


@app.get("/api/residents/{rid}/graph")
def graph(rid: str, request: Request):
    """The Reef's nodes. Snags are left out; whatever grew from one starts its own branch."""
    client(request)
    return [{**{k: p[k] for k in ("id", "source_id", "shift_id", "kind", "vote", "image", "created_at", "prompt")}, "color": media.avg_color(p["image"]) if p["image"] else "#c9a27a"}
            for p in db.posts(f"resident_id=? AND {LIVE}", (rid,), order="id", limit=100000)]


# ---------- posts & votes ----------
@app.get("/api/posts")
def list_posts(request: Request, resident: str = "", kind: str = "", days: float = 0, limit: int = 300, snagged: bool = False):
    """Posts, newest first. Snags only with snagged=true (the Ledger's "snagged" filter), and then only snags."""
    client(request)
    where, args = ["snagged_at IS NOT NULL" if snagged else LIVE], []
    if resident:
        where.append("resident_id=?"); args.append(resident)  # noqa: E702
    if kind:
        where.append("kind=?"); args.append(kind)  # noqa: E702
    if days:
        where.append("(created_at>=? OR kind='seed')"); args.append(time.time() - days * 86400)  # noqa: E702
    return [post_json(p) for p in db.posts(" AND ".join(where), tuple(args), limit=limit)]


@app.get("/api/posts/{pid}")
def get_post(pid: int, request: Request):
    client(request)
    p = db.post(pid)
    if not p:
        raise HTTPException(404)
    return {**post_json(p), "chain": [post_json(c) for c in db.chain(p)]}


@app.post("/api/votes")
def votes(request: Request, body: dict = Body(...)):
    who = client(request)
    for v in body.get("votes", []):
        db.vote(int(v["id"]), int(v["vote"]), who["name"], v.get("at"))
    return {"ok": True}


@app.post("/api/posts/{pid}/snag")
def snag(pid: int, request: Request, body: dict = Body(...)):
    """{snag: true} takes a broken render out of everything; {snag: false} undoes it,
    until the next night shift clears the files (then a 409)."""
    client(request)
    p = db.post(pid)
    if not p or p["kind"] != "edit":
        raise HTTPException(404, "only edits can be snagged")
    if body.get("snag", True):
        db.snag(pid)
    elif p["snagged_at"] and not db.unsnag(pid):
        raise HTTPException(409, "too late to undo: its images were cleared at the last night shift")
    return post_json(db.post(pid))


# ---------- shifts & journal ----------
@app.post("/api/shift")
def run_shift(request: Request):
    owner(request)
    return engine.submit("shift", trigger="manual")


@app.post("/api/shutdown")
def shutdown(request: Request):
    """Stop the Angler (`baitbook stop`). Only from this machine itself, never a paired device."""
    if not is_local(request):
        raise HTTPException(403, "only from the Angler's own machine")
    stop = getattr(app.state, "stop", None)
    if not stop:
        raise HTTPException(503, "this Angler wasn't started in a way that can stop itself")
    threading.Timer(0.3, stop).start()  # answer first
    return {"ok": True}


@app.get("/api/shifts")
def shifts(request: Request):
    client(request)
    return db.shifts()


@app.get("/api/journal")
def journal(request: Request):
    client(request)
    return db.events()


# ---------- pairing & docking (the phone) ----------
@app.post("/api/pair")
def pair(body: dict = Body(...)):
    """Code → token. After PAIR_TRIES wrong codes the code changes and pairing pauses."""
    with _pair_lock:
        if not _pair["code"]:
            _new_code()
        if time.time() < _pair["paused_until"]:
            raise HTTPException(429, "too many wrong codes; pairing is paused for a minute")
        if str(body.get("code", "")).strip() != _pair["code"]:
            _pair["fails"] += 1
            if _pair["fails"] >= PAIR_TRIES:
                _new_code()
                _pair["paused_until"] = time.time() + PAIR_PAUSE_S
                log.warning("%s wrong pairing codes: new code %s, pairing paused for %ss", PAIR_TRIES, _pair["code"], PAIR_PAUSE_S)
                db.event("device", f"{PAIR_TRIES} wrong pairing codes in a row: the code was changed and pairing paused for a minute.")
            raise HTTPException(403, "wrong pairing code")
        _pair["fails"] = 0
    role = "owner" if body.get("role") == "owner" else "device"
    token = secrets.token_urlsafe(24)
    name = re.sub(r"[^\w .-]", "", str(body.get("name") or ("PC" if role == "owner" else "phone")))[:40] or "phone"
    db.add_device(token, name, role)
    db.event("device", f"paired {'a PC app as the owner' if role == 'owner' else 'a new device'}: {name}.")
    return {"token": token, "name": name, "role": role}


@app.post("/api/unpair")
def unpair(request: Request):
    """A device forgetting itself: its token stops working."""
    who = client(request)
    if who["token"]:
        db.remove_device(token=who["token"])
        db.event("device", f"{who['name']} unpaired itself.")
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
def remove_device(device_id: str, request: Request):
    owner(request)
    name = db.remove_device(device_id=device_id)
    if name is None:
        raise HTTPException(404, "no such paired device: removed already?")
    db.event("device", f"removed paired device {name}.")
    return {"ok": True, "devices": db.devices()}


@app.post("/api/dock")
def dock(request: Request, body: dict = Body(...)):
    """One round trip: the phone's queued votes and snags come home; the catch goes out.
    Each queued item is {id, vote?, voted_at, snag?, unsnag?, snagged_at}: a vote (when present),
    and a snag or the undoing of one an earlier dock may have taken home, each with when it was
    made. An older phone app sends {id, at, vote?, snag?}: one time for both."""
    who = client(request)
    up = body.get("votes", [])
    n_votes = n_snags = n_undone = 0
    for v in up:
        if v.get("vote") is not None:
            db.vote(int(v["id"]), int(v["vote"]), who["name"], v.get("voted_at", v.get("at")))
            n_votes += 1
        if v.get("snag"):
            db.snag(int(v["id"]), v.get("snagged_at", v.get("at")))
            n_snags += 1
        elif v.get("unsnag"):
            n_undone += db.unsnag(int(v["id"]), v.get("snagged_at"))
    keep_from = time.time() - int(config.section("device").get("keep_nights", 7)) * 86400
    keep = {p["id"]: p for p in db.posts(f"(kind='seed' OR created_at>=?) AND vote>=0 AND {LIVE}", (keep_from,), limit=100000)}
    have = {int(i) for i in body.get("have", [])}
    new = [post_json(p) for pid, p in keep.items() if pid not in have]
    drop = sorted(have - set(keep))
    votes = {pid: keep[pid]["vote"] for pid in have & set(keep)}  # so votes made elsewhere reach the phone
    if who["token"]:
        db.record_dock(who["token"], len(up), len(new))
    n = lambda k, w: f"{k} {w}{'' if k == 1 else 's'}"  # noqa: E731
    snags = (f", {n(n_snags, 'snag')}" if n_snags else "") + (f", {n(n_undone, 'snag')} undone" if n_undone else "")
    db.event("dock", f"{who['name']} docked: {n(n_votes, 'vote')}{snags} home, {n(len(new), 'post')} out, {len(drop)} dropped.")
    # ready residents, and any other whose posts go out (one being read again), so no post comes without its resident
    sent = {p["resident_id"] for p in keep.values()}
    return {"residents": [{**r, "seeds": None} for r in db.residents() if r["status"] == "ready" or r["id"] in sent],
            "posts": new, "drop": drop, "votes": votes, "keep_nights": int(config.section("device").get("keep_nights", 7)), "now": time.time()}


# ---------- media & clients ----------
@app.get("/media/{name}")
def get_media(name: str, request: Request):
    client(request)
    p = media.path(name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})


def shell_version() -> str:
    """A hash of everything the phone app's service worker can cache (web/device/, web/shared/)."""
    h = hashlib.sha256()
    for path in sorted(p for folder in ("device", "shared") for p in (WEB / folder).rglob("*") if p.is_file()):
        h.update(path.relative_to(WEB).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()[:12]


@app.get("/device/sw.js")
def service_worker():
    """sw.js with VERSION set to shell_version(): change any shell file and a phone's next
    launch at home installs the new shell (see web/device/sw.js). Ahead of the /device mount."""
    js = (WEB / "device" / "sw.js").read_text(encoding="utf-8")
    js = js.replace("const VERSION = 'dev';", f"const VERSION = '{shell_version()}';", 1)
    return Response(js, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
def root():
    return RedirectResponse("/app/")


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


for mount, folder in (("/app", "app"), ("/device", "device"), ("/shared", "shared")):
    path = WEB / folder
    path.mkdir(parents=True, exist_ok=True)
    app.mount(mount, StaticFiles(directory=path, html=True), name=folder)


def device_index() -> Path:
    return WEB / "device" / "index.html"
