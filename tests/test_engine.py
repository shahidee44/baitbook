"""angler/engine.py with a fake language model and a fake ComfyUI: jobs, the schedule, a whole night shift."""

from __future__ import annotations

import random
import threading
from datetime import datetime

import pytest

from angler import config, db, media
from angler import prompts as P
from angler.engine import Engine, slug
from tests.support import add_edit, add_resident, wait_idle


def backdate(seconds: float = 60) -> None:
    """Make every post older than `now`, so a shift starting now can use them as sources
    (time.time() can tick coarsely on Windows)."""
    db.run("UPDATE posts SET created_at = created_at - ?", (seconds,))


def events(kind: str | None = None) -> list[str]:
    return [e["text"] for e in db.events(500) if kind is None or e["kind"] == kind]


@pytest.fixture
def gate(monkeypatch):
    """Holds a job inside the worker until the test lets it go: gate.hold("_run_shift")."""

    class Gate:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def hold(self, name: str, then=None):
            def held(engine_self, *a, **k):
                self.entered.set()
                assert self.release.wait(10), "the test never released the job"
                return then(engine_self, *a, **k) if then else None

            monkeypatch.setattr(Engine, name, held)
            return self

    g = Gate()
    yield g
    g.release.set()


# ---------- start-up ----------
def test_recover_marks_running_shifts_interrupted_and_requeues_reads(make_engine, fakes):
    config.load()
    sid = db.start_shift("schedule")
    add_resident("moss", status="reading", n_seeds=2)
    add_resident("reed", status="ready", n_seeds=1)
    e = make_engine()
    assert db.shifts()[0]["status"] == "interrupted" and db.shifts()[0]["id"] == sid
    assert any("cut off by the last stop" in t for t in events("shift"))
    assert any("@moss was cut off" in t for t in events("resident"))
    wait_idle(e)
    assert db.resident("moss")["status"] == "ready"  # the read ran again (with the fake model)
    assert db.resident("reed")["status"] == "ready"


# ---------- the vision read ----------
def test_read_with_the_fake_model(engine, fakes):
    lm, comfy = fakes
    add_resident("moss", status="reading", n_seeds=3)
    for s in db.seeds("moss"):
        db.update_post(s["id"], prompt="")
    assert engine.submit("read", resident_id="moss") == {"ok": True}
    wait_idle(engine)
    r = db.resident("moss")
    assert r["status"] == "ready" and r["moods"] == ["hushed", "sun-warmed", "domestic"]
    seeds = db.seeds("moss")
    assert all(s["prompt"].startswith("a weathered lighthouse") for s in seeds)  # clean_reply took off 'Prompt: "…"'
    assert all(s["tags"] == ["hushed", "sunwarmed", "domestic"] for s in seeds)
    assert comfy.released >= 1  # the image model was asked to give the GPU back first
    # one conversation: the system prompt, then one image per turn, then the mood ask
    last = lm.calls[-1]
    assert last[0]["role"] == "system"
    images = [m for m in last if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(images) == 3 and images[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert last[-1]["content"] == P.MOOD_ASK
    assert engine.snapshot()["state"] == "idle"


def test_failed_read_does_not_block_read_again(engine, monkeypatch):
    add_resident("moss", status="reading", n_seeds=1)
    real, calls = Engine._read_seeds, []

    def flaky(self, res, seeds):  # fails the first time, then reads with the fake model
        calls.append(res["id"])
        if len(calls) == 1:
            raise RuntimeError("model file missing")
        return real(self, res, seeds)

    monkeypatch.setattr(Engine, "_read_seeds", flaky)
    engine.submit("read", resident_id="moss")
    wait_idle(engine)
    snap = engine.snapshot()
    assert snap["state"] == "error" and snap["job"] == "read" and "model file missing" in snap["error"]
    assert db.resident("moss")["status"] == "failed"
    assert any("read failed: model file missing" in t for t in events("error"))
    assert engine.queued_read("moss") is False  # so "Read again" is allowed

    assert engine.submit("read", resident_id="moss")["ok"]
    wait_idle(engine)
    assert calls == ["moss", "moss"]
    assert db.resident("moss")["status"] == "ready"
    assert engine.snapshot()["state"] == "idle"


def test_a_read_never_lands_on_a_resident_made_again_under_the_same_handle(engine, fakes):
    """@moss is deleted while its seed images are read, and made again under the same handle."""
    lm, _ = fakes
    add_resident("moss", status="reading", n_seeds=2)
    real = lm.answer

    def answer(msgs):
        if msgs[-1]["content"] == P.MOOD_ASK:
            for stem in db.delete_resident("moss"):
                media.delete(stem)
            add_resident("moss", status="reading", n_seeds=1, moods=("new",))
        return real(msgs)

    lm.answer = answer
    engine.submit("read", resident_id="moss")
    wait_idle(engine)
    r = db.resident("moss")
    assert r["status"] == "reading" and r["moods"] == ["new"]  # the new @moss waits for its own read
    assert db.seeds("moss")[0]["tags"] == []
    assert engine.snapshot()["state"] == "idle" and not events("error")


def test_a_resident_deleted_mid_read_is_not_an_error(engine, fakes):
    lm, _ = fakes
    add_resident("moss", status="reading", n_seeds=2)
    real = lm.answer

    def answer(msgs):  # deleted after the first image: the second one's file is gone
        for stem in db.delete_resident("moss"):
            media.delete(stem)
        return real(msgs)

    lm.answer = answer
    engine.submit("read", resident_id="moss")
    wait_idle(engine)
    assert db.resident("moss") is None
    assert engine.snapshot()["state"] == "idle" and not events("error")


def test_queued_read_while_queued_and_running(engine, gate):
    add_resident("moss", status="reading", n_seeds=1)
    add_resident("reed", status="reading", n_seeds=1)
    gate.hold("_read_seeds")
    engine.submit("read", resident_id="moss")
    assert gate.entered.wait(5)
    engine.submit("read", resident_id="reed")
    assert engine.queued_read("moss")  # running
    assert engine.queued_read("reed")  # queued behind it
    assert not engine.queued_read("kelp")
    assert [j["resident_id"] for j in engine.snapshot()["queued"]] == ["reed"]
    gate.release.set()
    wait_idle(engine)
    assert not engine.queued_read("moss") and not engine.queued_read("reed")


# ---------- one shift at a time ----------
def test_duplicate_shift_refused_while_queued(engine, gate):
    add_resident("moss", status="reading", n_seeds=1)
    gate.hold("_read_seeds")
    engine.submit("read", resident_id="moss")  # keeps the worker busy
    assert gate.entered.wait(5)
    assert engine.submit("shift", trigger="manual") == {"ok": True}
    refused = engine.submit("shift", trigger="schedule")
    assert refused["ok"] is False and "already queued or running" in refused["reason"]
    assert [j["kind"] for j in engine.snapshot()["queued"]] == ["shift"]
    gate.release.set()
    wait_idle(engine)
    assert len(db.shifts()) == 1


def test_duplicate_shift_refused_while_running(engine, gate):
    gate.hold("_run_shift", then=lambda self, sid, trigger: 0)
    assert engine.submit("shift", trigger="manual")["ok"]
    assert gate.entered.wait(5)
    assert engine.submit("shift", trigger="manual")["ok"] is False
    gate.release.set()
    wait_idle(engine)
    assert engine.submit("shift", trigger="manual")["ok"] is True  # done: the next one is welcome
    wait_idle(engine)


def test_duplicate_shift_refused_while_starting(engine):
    """The worker has taken the job but the shift hasn't reported in yet."""
    with engine.lock:
        engine.status.update(state="starting", job="shift")
    assert engine.submit("shift")["ok"] is False


def test_failed_shift_does_not_block_the_next(engine, monkeypatch):
    calls = []

    def run_shift(self, sid, trigger):
        calls.append(trigger)
        if len(calls) == 1:
            raise RuntimeError("ComfyUI exploded")
        return 0

    monkeypatch.setattr(Engine, "_run_shift", run_shift)
    assert engine.submit("shift", trigger="schedule")["ok"]
    wait_idle(engine)
    snap = engine.snapshot()
    assert snap["state"] == "error" and snap["job"] == "shift" and "ComfyUI exploded" in snap["error"]
    first = db.shifts()[0]
    assert (first["status"], first["error"], first["trigger"]) == ("failed", "ComfyUI exploded", "schedule")
    assert any("shift failed: ComfyUI exploded" in t for t in events("error"))

    assert engine.submit("shift", trigger="manual")["ok"]  # the failed one doesn't count as running
    wait_idle(engine)
    assert calls == ["schedule", "manual"]
    assert engine.snapshot()["state"] == "idle"


def test_failed_job_marks_unfinished_items_stopped(engine, monkeypatch):
    def run_shift(self, sid, trigger):
        self._set(state="editing", items=[{"stage": "packed"}, {"stage": "editing"}, {"stage": "written"}, {"stage": "failed"}])
        raise RuntimeError("power cut")

    monkeypatch.setattr(Engine, "_run_shift", run_shift)
    engine.submit("shift")
    wait_idle(engine)
    assert [i["stage"] for i in engine.snapshot()["items"]] == ["packed", "stopped", "stopped", "failed"]


def test_snapshot_shape(engine):
    s = engine.snapshot()
    assert {"state", "job", "phase", "detail", "items", "error", "queued", "next_run"} <= set(s)
    assert s["next_run"] == "03:00"  # the example config's schedule
    config.update({"shift": {"schedule_enabled": False}})
    assert engine.snapshot()["next_run"] is None


# ---------- the schedule ----------
SCHED = {"schedule_enabled": True, "schedule_time": "03:00", "catch_up_hours": 6.0}


def t(h: int, m: int = 0, s: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 1, day, h, m, s)  # mid-January: no daylight-saving change nearby


@pytest.mark.parametrize("now, sched, expected", [
    (t(3, 5), {}, t(3)),
    (t(3, 0, 0), {}, t(3)),  # on the dot
    (t(2, 59), {}, None),  # before today's time; yesterday's is long past its window
    (t(8, 59, 59), {}, t(3)),  # catching up, within 6 h
    (t(9, 0, 1), {}, None),  # past the window
    (t(3, 0, 30), {"catch_up_hours": 0}, t(3)),  # 0 hours means one minute
    (t(3, 1, 1), {"catch_up_hours": 0}, None),
    (t(3, 0, 59), {"catch_up_hours": 0.001}, t(3)),  # never less than a minute
    (t(2, 59), {"catch_up_hours": 24}, t(3, day=14)),  # slept through yesterday's, a long window
    (t(0, 15, day=16), {"schedule_time": "23:30"}, t(23, 30, day=15)),  # across midnight
    (t(23, 45), {"schedule_time": "23:30"}, t(23, 30)),
    (t(3, 5), {"schedule_enabled": False}, None),
    (t(3, 5), {"schedule_time": "3:5"}, t(3, 5)),
])
def test_due(now, sched, expected):
    assert Engine.due(now, {**SCHED, **sched}) == expected


def test_due_defaults():
    assert Engine.due(t(3, 10), {"schedule_enabled": True}) == t(3)  # 03:00, 6 h
    assert Engine.due(t(3, 10), {}) is None  # off unless enabled


def test_due_is_settled_by_a_shift_started_since():
    db.run("INSERT INTO shifts(started_at,status,trigger) VALUES(?,?,?)", (t(3).timestamp() - 3600, "done", "manual"))
    assert Engine.due(t(4), SCHED) == t(3)  # one from before the scheduled time doesn't count
    db.run("INSERT INTO shifts(started_at,status,trigger) VALUES(?,?,?)", (t(3, 20).timestamp(), "failed", "manual"))
    assert Engine.due(t(4), SCHED) is None  # any shift since, even a failed manual one, settles the night
    assert Engine.due(t(3, 10), SCHED) is None  # (it's looked up by start time, not by the clock now)
    assert Engine.due(t(3, 5, day=16), SCHED) == t(3, day=16)  # the next night is owed again


# ---------- tonight's examples ----------
@pytest.fixture
def voted():
    res = add_resident("moss", n_seeds=2)
    seed = db.seeds("moss")[0]["id"]
    db.add_post(resident_id="moss", kind="seed", image=None, prompt="", shift_id=0)  # a seed not read yet
    bites = [add_edit("moss", seed, vote=1, prompt=f"bite {i}") for i in range(7)]
    throws = [add_edit("moss", seed, vote=-1, prompt=f"throw {i}") for i in range(5)]
    snagged = [add_edit("moss", seed, vote=1, snagged=True), add_edit("moss", seed, vote=-1, snagged=True)]
    add_edit("moss", seed)  # not voted
    other = add_resident("reed", n_seeds=1)
    add_edit("reed", db.seeds("reed")[0]["id"], vote=1, prompt="reed's bite")
    return res, set(bites), set(throws), set(snagged), other


def test_context_samples_the_configured_numbers(engine, voted):
    res, bites, throws, snagged, _ = voted
    ctx = engine.context(res, random.Random(1))
    assert (ctx["n_good"], ctx["n_bad"]) == (5, 3)
    assert len(ctx["good"]) == 5 and {p["id"] for p in ctx["good"]} <= bites
    assert len(ctx["bad"]) == 3 and {p["id"] for p in ctx["bad"]} <= throws
    assert ctx["base"] == [s["prompt"] for s in db.seeds("moss") if s["prompt"]] and len(ctx["base"]) == 2
    assert "## Bad examples: 3 random prompts" in ctx["system"]
    for p in ctx["good"]:
        assert f"- {p['prompt']}" in ctx["system"]
    assert "reed's bite" not in ctx["system"]


def test_context_takes_all_when_there_are_fewer(engine, voted):
    res, bites, throws, snagged, _ = voted
    config.update({"shift": {"good_examples": 10, "bad_examples": 10}})
    ctx = engine.context(res, random.Random(2))
    assert {p["id"] for p in ctx["good"]} == bites and {p["id"] for p in ctx["bad"]} == throws
    assert not ({p["id"] for p in ctx["good"] + ctx["bad"]} & snagged)


def test_context_without_bad_examples(engine, voted):
    res, *_ = voted
    config.update({"shift": {"good_examples": 0, "bad_examples": 0}})
    ctx = engine.context(res, random.Random(3))
    assert ctx["good"] == [] and ctx["bad"] == [] and (ctx["n_good"], ctx["n_bad"]) == (0, 0)
    assert "Bad examples" not in ctx["system"]
    assert "(none yet: stay close to the base prompts)" in ctx["system"]


def test_context_varies_with_the_rng(engine, voted):
    res, *_ = voted
    picks = {tuple(sorted(p["id"] for p in engine.context(res, random.Random(s))["good"])) for s in range(20)}
    assert len(picks) > 1


# ---------- write_edit: one more try when it comes back too close ----------
def test_write_edit_sends_a_too_close_prompt_back_once(engine, fakes):
    lm, _ = fakes
    src = {"id": 7, "prompt": "a red fox sitting in deep snow under a pale winter sky"}
    answers = iter([src["prompt"], "a heron wading through a neon-lit canal at midnight"])
    lm.answer = lambda msgs: next(answers)
    msgs = [{"role": "system", "content": "sys"}]
    prompt, directions, sim = engine.write_edit(msgs, src, [], random.Random(1), P.variation("bold"))
    assert prompt == "a heron wading through a neon-lit canal at midnight" and sim < 0.75
    assert len(directions) == 2
    retry = lm.calls[1]
    assert retry[-1]["content"].startswith("That prompt is 100% the same")
    # the rejected try stays out of the conversation
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"] and msgs[-1]["content"] == prompt


def test_write_edit_keeps_the_first_when_the_retry_is_no_better(engine, fakes):
    lm, _ = fakes
    src = {"id": 7, "prompt": "a red fox sitting in deep snow"}
    lm.answer = lambda msgs: src["prompt"]
    msgs = []
    prompt, _, sim = engine.write_edit(msgs, src, [], random.Random(1), P.variation("subtle"))
    assert prompt == src["prompt"] and sim == 1.0 and len(lm.calls) == 2


def test_write_edit_checks_tonights_other_prompts_too(engine, fakes):
    lm, _ = fakes
    near = ["a lighthouse at dusk with gulls overhead"]
    answers = iter([near[0], "something else entirely, a desert tram"])
    lm.answer = lambda msgs: next(answers)
    prompt, _, _ = engine.write_edit([], {"id": 1, "prompt": "a fox"}, near, random.Random(1), P.variation("wild"))
    assert prompt == "something else entirely, a desert tram"


def test_write_edit_accepts_a_fresh_prompt_first_time(engine, fakes):
    lm, _ = fakes
    prompt, directions, sim = engine.write_edit([], {"id": 1, "prompt": "a fox in snow"}, [], random.Random(1), P.variation("wild"))
    assert len(lm.calls) == 1 and len(directions) == 3 and sim < 0.6 and prompt


# ---------- a whole night shift ----------
@pytest.fixture
def night():
    """moss: 2 seeds. reed: 1 seed, a thrown-back edit and a snagged edit (neither is a source).
    kelp: its read failed. fern: no images at all. Every post is a minute old."""
    config.load()
    add_resident("moss", n_seeds=2, moods=("Hushed", "sun warmed", "salt", "late", "fifth"))
    add_resident("reed", n_seeds=1)
    reed_seed = db.seeds("reed")[0]["id"]
    last_night = db.start_shift("schedule")
    db.finish_shift(last_night, "done", 2)
    thrown = add_edit("reed", reed_seed, vote=-1, shift_id=last_night)
    snagged = add_edit("reed", reed_seed, snagged=True, shift_id=last_night)
    add_resident("kelp", status="failed", n_seeds=1)
    add_resident("fern", n_seeds=1, with_images=False)
    backdate()
    snag_stem = db.post(snagged)["image"]
    config.update({"shift": {"posts_per_resident": 3}})
    return {"thrown": thrown, "snagged": snagged, "snag_stem": snag_stem, "moss_stems": [s["image"] for s in db.seeds("moss")],
            "sources": {"moss": {s["id"] for s in db.seeds("moss")}, "reed": {reed_seed}}}


def test_run_shift_end_to_end(engine, fakes, night):
    lm, comfy = fakes
    sid = db.start_shift("manual")
    made = engine._run_shift(sid, "manual")
    assert made == 6

    # the snag was purged at the start: its files are gone, its row stays
    assert db.post(night["snagged"])["image"] is None
    assert not any(config.IMAGES.glob(f"{night['snag_stem']}*"))
    assert "cleared the images of 1 snagged post." in events("shift")

    edits = db.posts("shift_id=? AND kind='edit'", (sid,), order="id")
    assert len(edits) == 6
    for res in ("moss", "reed"):
        mine = [p for p in edits if p["resident_id"] == res]
        assert len(mine) == 3
        assert {p["source_id"] for p in mine} <= night["sources"][res]
    assert {p["source_id"] for p in edits if p["resident_id"] == "moss"} == night["sources"]["moss"]  # different sources first
    for p in edits:
        assert p["prompt"].startswith("zebra") and p["caption"].startswith("out by the water again")
        assert "\n" not in p["caption"] and p["seed"] and p["vote"] == 0
        assert media.path(f"{p['image']}.jpg") and media.path(f"{p['image']}_t.jpg") and media.path(f"{p['image']}.png")
    assert edits[0]["tags"] == ["hushed", "sunwarmed", "salt", "late"]  # the resident's moods, slugged, at most 4

    shift = db.shifts()[0]
    assert (shift["id"], shift["status"], shift["n_posts"], shift["error"]) == (sid, "done", 6, None)
    assert any(f"night shift #{sid}: 6 of 6 edits done" in t for t in events("shift"))
    assert any(f"night shift #{sid} started (manual): 6 edits across 2 residents, bold variation." in t for t in events("shift"))

    # phase 1 then phase 2: the language model unloads before ComfyUI starts
    assert lm.loads == 1 and lm.unloads >= 1 and comfy.ensured == 1 and len(comfy.edits) == 6
    status = engine.snapshot()
    assert status["state"] == "editing" and status["shift_id"] == sid
    assert [i["stage"] for i in status["items"]] == ["packed"] * 6
    assert all(i["post_id"] and i["image"] and i["change"] for i in status["items"])
    # every edit was asked of the right source image
    pngs = {s: media.png(db.post(s)["image"]) for s in night["sources"]["moss"] | night["sources"]["reed"]}
    for (source_png, prompt, _seed), item in zip(comfy.edits, status["items"], strict=True):
        assert source_png == pngs[item["source_id"]] and prompt == item["prompt"]


def test_run_shift_through_the_worker(engine, fakes, night):
    assert engine.submit("shift", trigger="manual")["ok"]
    wait_idle(engine)
    s = db.shifts()[0]
    assert (s["status"], s["n_posts"], s["trigger"]) == ("done", 6, "manual")
    assert engine.snapshot()["state"] == "idle"
    _, comfy = fakes
    assert comfy.released >= 1  # the image model is let go when the shift ends


def test_run_shift_counts_only_the_edits_that_worked(engine, fakes, night, monkeypatch):
    lm, comfy = fakes
    comfy.fail_prompts = ("zebra3 ",)  # moss's second edit: the fake's calls go prompt 1, caption 2, prompt 3, ...
    sid = db.start_shift("manual")
    assert engine._run_shift(sid, "manual") == 5
    items = engine.snapshot()["items"]
    failed = [i for i in items if i["stage"] == "failed"]
    assert len(failed) == 1 and "fake" in failed[0]["error"]
    assert db.shifts()[0]["n_posts"] == 5 and db.shifts()[0]["status"] == "done"


def test_run_shift_skips_an_empty_prompt(engine, fakes, night):
    lm, comfy = fakes
    lm.answer = lambda msgs: "" if msgs[-1]["content"] != P.CAPTION_ASK else "never asked"
    sid = db.start_shift("manual")
    assert engine._run_shift(sid, "manual") == 0
    assert comfy.edits == []
    assert all(i["stage"] == "failed" and i["caption"] == "" for i in engine.snapshot()["items"])
    assert not any(P.CAPTION_ASK == c[-1]["content"] for c in lm.calls)


def test_run_shift_keeps_nothing_of_a_resident_deleted_while_it_rendered(engine, fakes, night):
    """The audit's case: the render finishes after its resident is gone. No post, and no image files."""
    _, comfy = fakes
    real = comfy.edit

    def edit_then_delete(source_png, prompt, seed):
        out = real(source_png, prompt, seed)
        if db.resident("moss"):
            for stem in db.delete_resident("moss"):
                media.delete(stem)
        return out

    comfy.edit = edit_then_delete
    pngs_before = {p.name for p in config.IMAGES.glob("*.png")}
    sid = db.start_shift("manual")
    assert engine._run_shift(sid, "manual") == 3  # reed's three
    items = engine.snapshot()["items"]
    moss = [i for i in items if i["resident"] == "moss"]
    assert [i["stage"] for i in moss] == ["failed"] * 3
    assert "deleted while it rendered" in moss[0]["error"] and moss[1]["error"] == moss[2]["error"] == "its resident was deleted"
    assert len(comfy.edits) == 4  # moss's first render, then reed's three: the other two of moss's never went to ComfyUI
    assert db.posts("resident_id='moss'") == []
    made = {f"{p['image']}.png" for p in db.posts("shift_id=?", (sid,))}
    moss_seeds = {f"{s}.png" for s in night["moss_stems"]}
    assert {p.name for p in config.IMAGES.glob("*.png")} == (pngs_before - moss_seeds - {f"{night['snag_stem']}.png"}) | made


def test_run_shift_with_nothing_to_do(engine, fakes):
    lm, comfy = fakes
    config.load()
    add_resident("kelp", status="failed", n_seeds=1)
    sid = db.start_shift("manual")
    assert engine._run_shift(sid, "manual") == 0
    assert lm.loads == 0 and comfy.ensured == 0
    assert db.shifts()[0]["status"] == "done" and db.shifts()[0]["n_posts"] == 0


def test_run_shift_never_uses_new_or_thrown_back_or_snagged_sources(engine, fakes, night):
    """Posts made after the shift started aren't sources either."""
    _, comfy = fakes
    sid = db.start_shift("manual")
    engine._run_shift(sid, "manual")
    sources = {p["source_id"] for p in db.posts("shift_id=?", (sid,))}
    assert night["thrown"] not in sources and night["snagged"] not in sources
    sid2 = db.start_shift("manual")
    engine._run_shift(sid2, "manual")
    tonight = {p["id"] for p in db.posts("shift_id=?", (sid,))}
    assert not ({p["source_id"] for p in db.posts("shift_id=?", (sid2,))} - tonight - night["sources"]["moss"] - night["sources"]["reed"])


def test_slug():
    assert slug("Sun-warmed  Dusk!") == "sunwarmeddusk"
    assert slug("ünï") == "n"
    assert slug("") == ""
