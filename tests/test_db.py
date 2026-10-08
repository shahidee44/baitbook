"""angler/db.py: votes, snags, chains, shifts, devices, residents."""

from __future__ import annotations

import sqlite3
import time

import pytest

from angler import config, db
from tests.support import add_edit, add_resident


@pytest.fixture
def res():
    return add_resident("moss", n_seeds=1)


@pytest.fixture
def seed(res):
    return db.seeds(res["id"])[0]


# ---------- vote ----------
def test_vote_newer_wins_older_loses(res, seed):
    pid = add_edit("moss", seed["id"])
    db.vote(pid, 1, "pc", at=1000.0)
    db.vote(pid, -1, "phone", at=900.0)  # a phone docking late with an older vote
    p = db.post(pid)
    assert (p["vote"], p["voted_at"], p["voted_by"]) == (1, 1000.0, "pc")
    db.vote(pid, -1, "phone", at=1100.0)
    p = db.post(pid)
    assert (p["vote"], p["voted_at"], p["voted_by"]) == (-1, 1100.0, "phone")
    db.vote(pid, 0, "pc", at=1100.0)  # the same moment: the later write wins
    assert db.post(pid)["vote"] == 0


def test_vote_without_time_is_now(res, seed):
    pid = add_edit("moss", seed["id"])
    before = time.time()
    db.vote(pid, 1, "pc")
    p = db.post(pid)
    assert p["vote"] == 1 and before <= p["voted_at"] <= time.time()


def test_vote_from_the_future_is_clamped(res, seed):
    pid = add_edit("moss", seed["id"])
    db.vote(pid, 1, "phone", at=time.time() + 30 * 86400)  # a phone clock a month ahead
    p = db.post(pid)
    assert p["voted_at"] <= time.time()
    db.vote(pid, -1, "pc")  # so the owner's next vote still wins
    assert db.post(pid)["vote"] == -1


@pytest.mark.parametrize("value, stored", [(5, 1), (1, 1), (0, 0), (-1, -1), (-9, -1), ("1", 1)])
def test_vote_value_is_clamped(res, seed, value, stored):
    pid = add_edit("moss", seed["id"])
    db.vote(pid, value, "pc")
    assert db.post(pid)["vote"] == stored


def test_vote_accepts_a_string_time(res, seed):
    pid = add_edit("moss", seed["id"])
    db.vote(pid, 1, "phone", at="1234.5")
    assert db.post(pid)["voted_at"] == 1234.5


# ---------- snags ----------
def test_snag_and_unsnag(res, seed):
    pid = add_edit("moss", seed["id"])
    db.snag(pid, at=500.0)
    assert db.post(pid)["snagged_at"] == 500.0
    db.snag(pid, at=600.0)  # snagging again keeps the first time
    assert db.post(pid)["snagged_at"] == 500.0
    assert db.unsnag(pid) is True
    assert db.post(pid)["snagged_at"] is None
    assert db.unsnag(pid) is False  # nothing to undo


def test_snag_only_takes_edits(res, seed):
    db.snag(seed["id"])
    assert db.post(seed["id"])["snagged_at"] is None


def test_snag_time_is_clamped_and_defaults_to_now(res, seed):
    a, b = add_edit("moss", seed["id"]), add_edit("moss", seed["id"])
    db.snag(a, at=time.time() + 86400)
    db.snag(b)
    now = time.time()
    assert db.post(a)["snagged_at"] <= now
    assert now - 5 < db.post(b)["snagged_at"] <= now


def test_purge_snags_and_count(res, seed):
    other = add_resident("reed", n_seeds=1)
    oseed = db.seeds(other["id"])[0]
    keep = add_edit("moss", seed["id"])
    s1 = add_edit("moss", seed["id"], snagged=True)
    s2 = add_edit("reed", oseed["id"], snagged=True)
    s3 = add_edit("moss", seed["id"], snagged=True, with_image=False)  # no image to clear
    assert db.count_snags() == 3
    assert db.count_snags("moss") == 2 and db.count_snags("reed") == 1 and db.count_snags("nobody") == 0
    expected = sorted([db.post(s1)["image"], db.post(s2)["image"]])

    assert sorted(db.purge_snags()) == expected
    assert all(db.post(p)["image"] is None for p in (s1, s2, s3))
    assert db.post(keep)["image"] is not None
    assert db.purge_snags() == []  # nothing left to clear
    # snags still count once their images are gone (the snag rate is snags / edits made)
    assert db.count_snags() == 3
    # and can't be undone any more
    assert db.unsnag(s1) is False
    assert db.post(s1)["snagged_at"] is not None


def test_purge_snags_returns_the_stems(res, seed):
    s1 = add_edit("moss", seed["id"], snagged=True)
    stem = db.post(s1)["image"]
    assert db.purge_snags() == [stem]


# ---------- chain ----------
def test_chain_is_seed_first(res, seed):
    e1 = add_edit("moss", seed["id"])
    e2 = add_edit("moss", e1)
    e3 = add_edit("moss", e2)
    assert [p["id"] for p in db.chain(db.post(e3))] == [seed["id"], e1, e2]
    assert [p["id"] for p in db.chain(db.post(e1))] == [seed["id"]]
    assert db.chain(db.post(seed["id"])) == []


def test_chain_survives_a_cycle_and_a_missing_source(res, seed):
    e1 = add_edit("moss", seed["id"])
    e2 = add_edit("moss", e1)
    db.update_post(e1, source_id=e2)  # corrupt: a loop
    chain = [p["id"] for p in db.chain(db.post(e2))]
    assert sorted(chain) == sorted({e1, e2}) and len(chain) == 2
    e3 = add_edit("moss", 99999)  # its source is gone
    assert db.chain(db.post(e3)) == []


def test_chain_passes_through_snags(res, seed):
    e1 = add_edit("moss", seed["id"], snagged=True)
    e2 = add_edit("moss", e1)
    db.purge_snags()
    assert [p["id"] for p in db.chain(db.post(e2))] == [seed["id"], e1]


# ---------- shifts ----------
def test_interrupt_shifts():
    a, b = db.start_shift("manual"), db.start_shift("schedule")
    c = db.start_shift("manual")
    db.finish_shift(c, "done", 3)
    assert db.interrupt_shifts() == 2
    rows = {s["id"]: s for s in db.shifts()}
    assert rows[a]["status"] == rows[b]["status"] == "interrupted"
    assert rows[a]["finished_at"] is not None
    assert rows[c]["status"] == "done" and rows[c]["n_posts"] == 3
    assert db.interrupt_shifts() == 0


def test_shift_since():
    db.run("INSERT INTO shifts(started_at,status,trigger) VALUES(?,?,?)", (1000.0, "done", "manual"))
    assert db.shift_since(999.0)
    assert db.shift_since(1000.0)
    assert not db.shift_since(1000.5)


def test_finish_shift_and_snag_counts_per_shift(res, seed):
    sid = db.start_shift("manual")
    add_edit("moss", seed["id"], shift_id=sid)
    add_edit("moss", seed["id"], shift_id=sid, snagged=True)
    db.finish_shift(sid, "failed", 2, "boom")
    s = db.shifts()[0]
    assert (s["status"], s["n_posts"], s["error"], s["n_snags"]) == ("failed", 2, "boom", 1)


# ---------- devices ----------
def test_devices_add_list_remove():
    db.add_device("tok-phone", "phone")
    db.add_device("tok-pc", "PC", role="owner")
    assert db.device("tok-phone")["role"] == "device"
    assert db.device("tok-pc")["role"] == "owner"
    assert db.device("nope") is None
    listed = db.devices()
    assert [d["name"] for d in listed] == ["phone", "PC"]
    assert all("token" not in d and isinstance(d["id"], str) and "tok" not in d["id"] for d in listed)

    assert db.remove_device(device_id=listed[0]["id"]) == "phone"
    assert db.device("tok-phone") is None and db.device("tok-pc") is not None
    assert db.remove_device() is None  # neither given: nothing happens
    assert db.remove_device(device_id="no-such-id") is None
    assert len(db.devices()) == 1
    assert db.remove_device(token="tok-pc") == "PC"
    assert db.devices() == []


def test_a_removed_devices_id_never_names_another():
    """The audit's case: a row id comes back once the last device is gone, so a second
    "remove" of the old one would have revoked the next device paired."""
    db.add_device("token-a", "old")
    old_id = db.devices()[0]["id"]
    assert db.remove_device(device_id=old_id) == "old"
    db.add_device("token-b", "new")
    assert db.devices()[0]["id"] != old_id
    assert db.remove_device(device_id=old_id) is None
    assert db.device("token-b") is not None


def test_record_dock():
    db.add_device("tok", "phone")
    db.record_dock("tok", 3, 7)
    assert db.device("tok")["last_dock_at"] is not None
    assert db.q("SELECT device, up, down FROM docks") == [{"device": "tok", "up": 3, "down": 7}]


# ---------- residents ----------
def test_delete_resident_returns_image_stems(res, seed):
    other = add_resident("reed", n_seeds=1)
    e1 = add_edit("moss", seed["id"])
    add_edit("moss", e1, with_image=False)
    stems = {p["image"] for p in db.posts("resident_id='moss'") if p["image"]}
    assert len(stems) == 2
    assert sorted(db.delete_resident("moss")) == sorted(stems)
    assert db.resident("moss") is None and db.posts("resident_id='moss'") == []
    assert db.resident("reed") is not None and len(db.seeds(other["id"])) == 1


def test_add_resident_with_its_seeds_is_all_or_nothing(res):
    db.add_resident("reed", "Reed", "reed", "", "#C7B8DA", seeds=["stem-a", "stem-b"])
    assert [(s["image"], s["kind"], s["shift_id"]) for s in db.seeds("reed")] == [("stem-a", "seed", 0), ("stem-b", "seed", 0)]
    with pytest.raises(sqlite3.IntegrityError):
        db.add_resident("moss", "Moss again", "moss again", "", "#C7B8DA", seeds=["stem-c"])  # the id is taken
    assert db.posts("image='stem-c'") == [] and db.resident("moss")["name"] == "Moss"


def test_add_edit_only_onto_the_resident_its_source_is_from(res, seed):
    edit = dict(resident_id="moss", kind="edit", source_id=seed["id"], prompt="p", tags=["hushed"], image="x", shift_id=1)
    pid = db.add_edit(**edit)
    assert db.post(pid)["source_id"] == seed["id"] and db.post(pid)["tags"] == ["hushed"]
    db.delete_resident("moss")
    assert db.add_edit(**edit) is None  # deleted
    add_resident("moss", n_seeds=1)
    assert db.add_edit(**edit) is None  # made again under the same handle: its posts are new
    assert db.add_edit(**{**edit, "source_id": db.seeds("moss")[0]["id"]})
    assert db.posts("kind='edit' AND image='x'", ()) and len(db.posts("kind='edit'")) == 1


def test_update_resident_with_a_seed_only_while_it_is_theirs(res, seed):
    assert db.update_resident("moss", seed=seed["id"], status="failed") is True
    assert db.update_resident("moss") is False  # nothing to change
    db.delete_resident("moss")
    assert db.update_resident("moss", seed=seed["id"], status="ready") is False
    add_resident("moss", status="reading", n_seeds=1)
    assert db.update_resident("moss", seed=seed["id"], status="ready", moods=["old"]) is False
    assert db.resident("moss")["status"] == "reading" and db.resident("moss")["moods"] != ["old"]


def test_unsnag_only_a_snag_made_before_the_undo(res, seed):
    pid = add_edit("moss", seed["id"])
    db.snag(pid, time.time() - 100)
    assert db.unsnag(pid, time.time() - 200) is False  # the undo is older than the snag
    assert db.post(pid)["snagged_at"] is not None
    assert db.unsnag(pid, time.time() - 50) is True
    assert db.post(pid)["snagged_at"] is None


def test_update_resident_and_moods_round_trip(res):
    db.update_resident("moss", moods=["a", "b"], name="Moss F", status="failed", evil="x")
    r = db.resident("moss")
    assert r["moods"] == ["a", "b"] and r["name"] == "Moss F" and r["status"] == "failed"
    db.update_resident("moss")  # nothing to change
    assert db.resident("moss")["name"] == "Moss F"


def test_post_tags_round_trip(res, seed):
    pid = db.add_post(resident_id="moss", kind="edit", source_id=seed["id"], tags=["x", "y"], image=None)
    assert db.post(pid)["tags"] == ["x", "y"]
    assert db.post(99999) is None


# ---------- schema ----------
def test_conn_migrates_an_old_database():
    """A database from the first build: devices without role, posts without snagged_at."""
    config.DATA.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(config.DB_PATH)
    old.executescript("""
      CREATE TABLE residents(id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL UNIQUE, bio TEXT DEFAULT '',
        hue TEXT DEFAULT '#E8C9A8', moods TEXT DEFAULT '[]', status TEXT DEFAULT 'reading', created_at REAL);
      CREATE TABLE posts(id INTEGER PRIMARY KEY AUTOINCREMENT, resident_id TEXT NOT NULL, kind TEXT NOT NULL,
        source_id INTEGER, prompt TEXT DEFAULT '', caption TEXT DEFAULT '', tags TEXT DEFAULT '[]',
        image TEXT, seed INTEGER, shift_id INTEGER DEFAULT 0, created_at REAL, vote INTEGER DEFAULT 0, voted_at REAL, voted_by TEXT);
      CREATE TABLE devices(token TEXT PRIMARY KEY, name TEXT, created_at REAL, last_dock_at REAL);
      INSERT INTO devices(token,name,created_at) VALUES('old-tok','old phone',1.0);
    """)
    old.commit()
    old.close()
    assert db.device("old-tok")["role"] == "device"  # every device paired back then was a phone
    db.add_resident("moss", "Moss", "moss", "", "#fff")
    pid = db.add_post(resident_id="moss", kind="edit", image="x")
    db.snag(pid)
    assert db.post(pid)["snagged_at"] is not None
    # a copy of it as it was came first
    [kept] = (config.DATA / "backups").glob("baitbook-*.db")
    copy = sqlite3.connect(kept)
    assert "role" not in {r[1] for r in copy.execute("PRAGMA table_info(devices)")}
    assert "snagged_at" not in {r[1] for r in copy.execute("PRAGMA table_info(posts)")}
    assert copy.execute("SELECT token, name FROM devices").fetchall() == [("old-tok", "old phone")]
    copy.close()


def test_conn_backs_up_only_before_a_change():
    db.add_resident("moss", "Moss", "moss", "", "#fff")  # a new database: made whole, nothing to keep
    db.conn().close()
    db._conn = None
    assert db.resident("moss")["name"] == "Moss"  # opened again: nothing changes either
    assert not (config.DATA / "backups").exists()
