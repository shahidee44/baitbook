"""SQLite: residents, posts (seeds and edits), votes, shifts, devices, journal."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS residents(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL UNIQUE, bio TEXT DEFAULT '',
  hue TEXT DEFAULT '#E8C9A8', moods TEXT DEFAULT '[]', status TEXT DEFAULT 'reading', created_at REAL);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, resident_id TEXT NOT NULL, kind TEXT NOT NULL,
  source_id INTEGER, prompt TEXT DEFAULT '', caption TEXT DEFAULT '', tags TEXT DEFAULT '[]',
  image TEXT, seed INTEGER, shift_id INTEGER DEFAULT 0, created_at REAL,
  vote INTEGER DEFAULT 0, voted_at REAL, voted_by TEXT, snagged_at REAL);
CREATE INDEX IF NOT EXISTS posts_res ON posts(resident_id);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, finished_at REAL, status TEXT,
  trigger TEXT, n_posts INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS devices(token TEXT PRIMARY KEY, name TEXT, created_at REAL, last_dock_at REAL, role TEXT DEFAULT 'device');
CREATE TABLE IF NOT EXISTS docks(id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, at REAL, up INTEGER, down INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, text TEXT, resident_id TEXT);
"""

# columns added since the first build: (table, column, its definition). A database made before
# one of them gets it in place, after a copy of it as it was (backup()).
ADDED = (("devices", "role", "TEXT DEFAULT 'device'"),  # every device paired then was a phone
         ("posts", "snagged_at", "REAL"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
log = logging.getLogger("angler.db")


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:  # once, even when two threads ask first at the same time
        if _conn is None:
            config.DATA.mkdir(exist_ok=True)
            c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            c.row_factory = sqlite3.Row
            # only a table there already can lack a column: SCHEMA makes a new one whole
            columns = lambda table: {r[1] for r in c.execute(f"PRAGMA table_info({table})")}  # noqa: E731
            missing = [(t, col, d) for t, col, d in ADDED if (have := columns(t)) and col not in have]
            if missing:
                backup(c, "before adding " + ", ".join(f"{t}.{col}" for t, col, _ in missing))
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            for t, col, d in missing:
                c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {d}")
            c.commit()
            _conn = c
        return _conn


def backup(c: sqlite3.Connection, why: str) -> Path:
    """Copy the database as it is now into data/backups/, before its schema changes. Even a
    source checkout runs on the owner's own data, so a change made there mustn't be able to
    cost them it. Just the database: the images aren't copied."""
    folder = config.DATA / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / f"baitbook-{time.strftime('%Y%m%d-%H%M%S')}.db"
    dest = sqlite3.connect(path)
    try:
        c.backup(dest)
    finally:
        dest.close()
    log.info("Copied the database to %s %s.", path, why)
    return path


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


# ---------- shaping ----------
def _resident(r: dict | None) -> dict | None:
    if r:
        r["moods"] = json.loads(r["moods"] or "[]")
    return r


def _post(p: dict | None) -> dict | None:
    if p:
        p["tags"] = json.loads(p["tags"] or "[]")
    return p


# ---------- residents ----------
def residents() -> list[dict]:
    return [_resident(r) for r in q("SELECT * FROM residents ORDER BY created_at")]


def resident(rid: str) -> dict | None:
    return _resident(one("SELECT * FROM residents WHERE id=?", (rid,)))


def add_resident(rid: str, name: str, handle: str, bio: str, hue: str, seeds: list[str] | None = None) -> None:
    """A new resident, with a seed post for each image stem in `seeds`: all of it or none."""
    now = time.time()
    with _lock, conn() as c:
        c.execute("INSERT INTO residents(id,name,handle,bio,hue,status,created_at) VALUES(?,?,?,?,?,'reading',?)", (rid, name, handle, bio, hue, now))
        c.executemany("INSERT INTO posts(resident_id,kind,image,shift_id,created_at) VALUES(?,'seed',?,0,?)", [(rid, stem, now) for stem in seeds or []])


def update_resident(rid: str, seed: int | None = None, **fields) -> bool:
    """True if a resident was changed. With `seed`, only while that post is still the resident's:
    a job can't write to a resident deleted since it started (or one made again under its
    handle, which gets new posts; post ids are never reused)."""
    if "moods" in fields:
        fields["moods"] = json.dumps(fields["moods"])
    keys = [k for k in fields if k in ("name", "handle", "bio", "hue", "moods", "status")]
    if not keys:
        return False
    guard, args = ("", ()) if seed is None else (" AND EXISTS(SELECT 1 FROM posts WHERE id=? AND resident_id=?)", (seed, rid))
    with _lock:
        cur = conn().execute(f"UPDATE residents SET {', '.join(k + '=?' for k in keys)} WHERE id=?{guard}", (*[fields[k] for k in keys], rid, *args))
        conn().commit()
        return cur.rowcount > 0


def delete_resident(rid: str) -> list[str]:
    """Delete a resident and every post of theirs, in one go; returns their image stems so the files can go."""
    with _lock, conn() as c:
        images = [r["image"] for r in c.execute("SELECT image FROM posts WHERE resident_id=? AND image IS NOT NULL", (rid,))]
        c.execute("DELETE FROM posts WHERE resident_id=?", (rid,))
        c.execute("DELETE FROM residents WHERE id=?", (rid,))
    return images


# ---------- posts ----------
def _row(p: dict) -> tuple[str, tuple]:
    p.setdefault("created_at", time.time())
    p["tags"] = json.dumps(p.get("tags", []))
    return ",".join(p), tuple(p.values())


def add_post(**p) -> int:
    cols, vals = _row(p)
    return run(f"INSERT INTO posts({cols}) VALUES({','.join('?' * len(vals))})", vals)


def add_edit(**p) -> int | None:
    """Add an edit while its source is still its resident's. None, and nothing added, if that
    resident was deleted while the edit rendered (and maybe made again under the same handle:
    the new one has new posts, so the old source tells them apart)."""
    cols, vals = _row(p)
    with _lock:
        cur = conn().execute(f"INSERT INTO posts({cols}) SELECT {','.join('?' * len(vals))} WHERE EXISTS(SELECT 1 FROM posts WHERE id=? AND resident_id=?)",
                             (*vals, p["source_id"], p["resident_id"]))
        conn().commit()
        return cur.lastrowid if cur.rowcount else None


def update_post(pid: int, **fields) -> None:
    keys = list(fields)
    run(f"UPDATE posts SET {', '.join(k + '=?' for k in keys)} WHERE id=?", (*[fields[k] for k in keys], pid))


def post(pid: int) -> dict | None:
    return _post(one("SELECT * FROM posts WHERE id=?", (pid,)))


def posts(where: str = "1=1", args: tuple = (), order: str = "id DESC", limit: int = 1000) -> list[dict]:
    return [_post(p) for p in q(f"SELECT * FROM posts WHERE {where} ORDER BY {order} LIMIT {int(limit)}", args)]


def seeds(rid: str) -> list[dict]:
    return posts("resident_id=? AND kind='seed'", (rid,), order="id ASC")


def vote(pid: int, value: int, by: str, at: float | None = None) -> None:
    """Set a vote made at `at` (now if not given). A vote older than the one stored
    loses: a phone that docks late can't undo what the owner decided since."""
    value = max(-1, min(1, int(value)))
    now = time.time()
    at = min(float(at), now) if at else now  # a phone clock running ahead can't win forever
    run("UPDATE posts SET vote=?, voted_at=?, voted_by=? WHERE id=? AND (voted_at IS NULL OR voted_at<=?)", (value, at, by, pid, at))


# ---------- snags: broken renders, not taste ----------
# A snagged post leaves every feed, is never a source or an example again, and
# its image files go at the start of the next night shift (until then it can be
# unsnagged). The row stays, so edit chains that pass through it still add up.
def snag(pid: int, at: float | None = None) -> None:
    run("UPDATE posts SET snagged_at=? WHERE id=? AND kind='edit' AND snagged_at IS NULL", (min(float(at or time.time()), time.time()), pid))


def unsnag(pid: int, at: float | None = None) -> bool:
    """Undo a snag made before `at` (now if not given), if its image is still there. False
    once the files are gone, or if the snag is newer than the undo (a phone docking late)."""
    at = min(float(at), time.time()) if at else time.time()
    with _lock:
        cur = conn().execute("UPDATE posts SET snagged_at=NULL WHERE id=? AND snagged_at<=? AND image IS NOT NULL", (pid, at))
        conn().commit()
        return cur.rowcount > 0


def count_snags(rid: str | None = None) -> int:
    """Every snag ever, its images cleared or not; for one resident if given."""
    where, args = ("kind='edit' AND snagged_at IS NOT NULL AND resident_id=?", (rid,)) if rid else ("kind='edit' AND snagged_at IS NOT NULL", ())
    return one(f"SELECT COUNT(*) AS n FROM posts WHERE {where}", args)["n"]


def purge_snags() -> list[str]:
    """Forget the images of every snagged post; returns their stems so the files can go."""
    with _lock:
        stems = [r["image"] for r in conn().execute("SELECT image FROM posts WHERE snagged_at IS NOT NULL AND image IS NOT NULL")]
        conn().execute("UPDATE posts SET image=NULL WHERE snagged_at IS NOT NULL")
        conn().commit()
        return stems


def chain(p: dict) -> list[dict]:
    """Every image `p` descends from, seed first."""
    out, seen = [], set()
    cur = post(p["source_id"]) if p.get("source_id") else None
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        out.insert(0, cur)
        cur = post(cur["source_id"]) if cur.get("source_id") else None
    return out


# ---------- journal, shifts, devices ----------
def event(kind: str, text: str, resident_id: str | None = None) -> None:
    run("INSERT INTO events(at,kind,text,resident_id) VALUES(?,?,?,?)", (time.time(), kind, text, resident_id))


def events(limit: int = 80) -> list[dict]:
    return q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


def interrupt_shifts() -> int:
    """Shifts still 'running' from before a restart; none of them is any more."""
    with _lock:
        cur = conn().execute("UPDATE shifts SET status='interrupted', finished_at=? WHERE status='running'", (time.time(),))
        conn().commit()
        return cur.rowcount


def shift_since(ts: float) -> bool:
    return one("SELECT id FROM shifts WHERE started_at>=? LIMIT 1", (ts,)) is not None


def start_shift(trigger: str) -> int:
    return run("INSERT INTO shifts(started_at,status,trigger) VALUES(?,?,?)", (time.time(), "running", trigger))


def finish_shift(sid: int, status: str, n_posts: int, error: str | None = None) -> None:
    run("UPDATE shifts SET finished_at=?, status=?, n_posts=?, error=? WHERE id=?", (time.time(), status, n_posts, error, sid))


def shifts(limit: int = 30) -> list[dict]:
    """The latest shifts, each with how many of its posts were snagged since (n_snags)."""
    return q("SELECT s.*, (SELECT COUNT(*) FROM posts p WHERE p.shift_id=s.id AND p.kind='edit' AND p.snagged_at IS NOT NULL) AS n_snags "
             "FROM shifts s ORDER BY s.id DESC LIMIT ?", (limit,))


def add_device(token: str, name: str, role: str = "device") -> None:
    run("INSERT INTO devices(token,name,created_at,role) VALUES(?,?,?,?)", (token, name, time.time(), role))


def device(token: str) -> dict | None:
    return one("SELECT * FROM devices WHERE token=?", (token,))


def _public_id(token: str) -> str:
    """A paired device's id in the apps. Made from its token, so it's never another device's
    (a row id can be, once the last device is removed), and it doesn't give the token away."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def devices() -> list[dict]:
    """Every paired device, without its token; `id` is what removing one takes."""
    return [{"id": _public_id(d.pop("token")), **d} for d in q("SELECT token, name, role, created_at, last_dock_at FROM devices ORDER BY created_at")]


def remove_device(device_id: str | None = None, token: str | None = None) -> str | None:
    """Forget a paired device, by its id from devices() or its token. Returns its name, or None if there was no such device."""
    with _lock:
        if device_id is not None:
            token = next((t for t in (r["token"] for r in q("SELECT token FROM devices")) if _public_id(t) == device_id), None)
        dev = device(token) if token else None
        if dev:
            run("DELETE FROM devices WHERE token=?", (token,))
        return dev and dev["name"]


def record_dock(token: str, up: int, down: int) -> None:
    now = time.time()
    run("UPDATE devices SET last_dock_at=? WHERE token=?", (now, token))
    run("INSERT INTO docks(device,at,up,down) VALUES(?,?,?,?)", (token, now, up, down))
