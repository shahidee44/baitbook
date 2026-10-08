"""The Angler's engine: one worker thread, one GPU job at a time.

Jobs
  read   — a new resident's vision read: one conversation with the language
           model, one seed image per turn (→ base prompts), then mood tags.
  shift  — a night shift, in series: the language model writes every edit
           prompt and caption for the night and unloads; then the image model
           edits every source image. Nothing runs both models at once.

A scheduler thread queues a shift at [shift] schedule_time when it's enabled,
or later if the machine slept through it (within [shift] catch_up_hours).
`status` is what the apps poll to draw progress.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import io
import logging
import queue
import random
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import config, db, media
from . import prompts as P
from .imagegen import ComfyClient
from .llm import LanguageModel, image_part

log = logging.getLogger("angler.engine")


def slug(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tag.lower())


_awake: subprocess.Popen | None = None  # macOS / Linux: the process holding off sleep


def keep_awake(on: bool) -> None:
    """Stop the machine idling into sleep while a job runs. Windows: for this thread;
    macOS: caffeinate; Linux: systemd-inhibit, where there is one."""
    global _awake
    if sys.platform == "win32":
        es_continuous, es_system_required = 0x80000000, 0x00000001
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))
        return
    if on and _awake is None:
        cmd = (["caffeinate", "-i", "-w", str(os.getpid())] if sys.platform == "darwin" else
               ["systemd-inhibit", "--what=idle:sleep", "--who=baitbook", "--why=a job is running", "sleep", "infinity"])
        if shutil.which(cmd[0]):
            with contextlib.suppress(OSError):
                _awake = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not on and _awake is not None:
        _awake.terminate()
        _awake = None


class Engine:
    def __init__(self) -> None:
        self.jobs: queue.Queue = queue.Queue()
        self.pending: list[dict] = []
        self.lock = threading.Lock()
        self.status: dict = self._idle()
        self._lm: LanguageModel | None = None
        self._lm_cfg: dict | None = None
        self._comfy: ComfyClient | None = None
        self._recover()
        threading.Thread(target=self._worker, name="angler-worker", daemon=True).start()
        threading.Thread(target=self._scheduler, name="angler-scheduler", daemon=True).start()

    def _recover(self) -> None:
        """Jobs live in memory, so whatever was running when the Angler stopped is over."""
        n = db.interrupt_shifts()
        if n:
            db.event("shift", f"{n} night shift{'s' if n > 1 else ''} cut off by the last stop, marked interrupted.")
        for r in db.residents():
            if r["status"] == "reading":
                self.submit("read", resident_id=r["id"])
                db.event("resident", f"the vision read of @{r['handle']} was cut off by the last stop; queued again.", r["id"])

    # ---------- plumbing ----------
    @staticmethod
    def _idle() -> dict:
        return {"state": "idle", "job": None, "phase": "", "detail": "", "items": [], "error": None, "updated": time.time()}

    def _set(self, **kw) -> None:
        with self.lock:
            self.status.update(kw, updated=time.time())

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.status)
            s["items"] = [dict(i) for i in self.status["items"]]
            s["queued"] = [dict(j) for j in self.pending]
        s["next_run"] = self.next_run()
        return s

    def _running(self, kind: str) -> bool:
        """A `kind` job is running now. A failed one keeps its job and items in the
        status, so the apps can show what stopped, but no longer counts. Hold the lock."""
        return self.status.get("job") == kind and self.status.get("state") not in ("idle", "error")

    def submit(self, kind: str, **args) -> dict:
        job = {"kind": kind, "args": args, "queued_at": time.time()}
        with self.lock:
            if kind == "shift" and (any(j["kind"] == "shift" for j in self.pending) or self._running("shift")):
                return {"ok": False, "reason": "a night shift is already queued or running"}
            self.pending.append({"kind": kind, **{k: v for k, v in args.items() if isinstance(v, (str, int))}})
        self.jobs.put(job)
        return {"ok": True}

    def lm(self) -> LanguageModel:
        cfg = config.section("text")
        if self._lm is None or cfg != self._lm_cfg:
            if self._lm:
                self._lm.unload()
            self._lm, self._lm_cfg = LanguageModel(cfg), cfg
        return self._lm

    def comfy(self) -> ComfyClient:
        cfg = config.section("image")
        cfg.setdefault("comfy_log", str(config.DATA / "comfy.log"))
        if self._comfy is None or self._comfy.cfg != cfg:
            self._comfy = ComfyClient(cfg)
        return self._comfy

    def _free_image_model(self) -> None:
        with contextlib.suppress(Exception):
            self.comfy().release()

    def _chat(self, msgs: list[dict], **kw) -> str:
        # the multimodal chat handler prints the whole rendered prompt; keep the console readable
        with contextlib.redirect_stdout(io.StringIO()):
            return self.lm().chat(msgs, **kw)

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            with self.lock:
                if self.pending:
                    self.pending.pop(0)
                # busy from here, so the same job can't be queued again before it reports in
                self.status.update(self._idle(), state="starting", job=job["kind"], resident=job["args"].get("resident_id"))
            keep_awake(True)
            try:
                if job["kind"] == "read":
                    self._read(job["args"]["resident_id"])
                elif job["kind"] == "shift":
                    self._shift(job["args"].get("trigger", "manual"))
                self._set(**self._idle())
            except Exception as exc:  # noqa: BLE001
                log.error("job failed: %s", traceback.format_exc())
                with self.lock:
                    for it in self.status["items"]:
                        if it.get("stage") not in ("packed", "done", "failed"):
                            it["stage"] = "stopped"
                self._set(state="error", error=str(exc), detail="")
                db.event("error", f"{job['kind']} failed: {exc}")
            finally:
                if self._lm:
                    self._lm.unload()
                keep_awake(False)

    def queued_read(self, rid: str) -> bool:
        with self.lock:
            return any(j["kind"] == "read" and j.get("resident_id") == rid for j in self.pending) or (
                self._running("read") and self.status.get("resident") == rid)

    # ---------- the vision read ----------
    def _read(self, rid: str) -> None:
        res = db.resident(rid)
        seeds = db.seeds(rid)
        if not res or not seeds:
            return
        db.update_resident(rid, status="reading")
        try:
            self._read_seeds(res, seeds)
        except Exception:
            # the worker logs it and writes the journal, unless the resident was deleted meanwhile (its files went with it)
            if db.update_resident(rid, seed=seeds[0]["id"], status="failed"):
                raise
            log.info("@%s was deleted while its seed images were read", res["handle"])

    def _read_seeds(self, res: dict, seeds: list[dict]) -> None:
        rid = res["id"]
        image_cfg, text_cfg = config.section("image"), config.section("text")
        items = [{"post_id": s["id"], "image": s["image"], "stage": "waiting", "prompt": ""} for s in seeds]
        items.append({"post_id": None, "image": None, "stage": "waiting", "prompt": "", "tags": True})
        self._set(state="reading", job="read", resident=rid, phase=f"{text_cfg['name']} is reading @{res['handle']}'s seed images", items=items, error=None)
        self._free_image_model()
        self._set(detail="loading the language model")
        self.lm().load()
        temp = float(text_cfg.get("read_temperature", 0.4))
        msgs = [{"role": "system", "content": P.read_system(image_cfg, len(seeds))}]
        for i, s in enumerate(seeds):
            items[i]["stage"] = "writing"
            self._set(items=items, detail=f"image {i + 1} of {len(seeds)}")
            msgs.append({"role": "user", "content": [image_part(media.png(s["image"])), {"type": "text", "text": P.read_ask(image_cfg, i + 1, len(seeds))}]})
            prompt = P.clean_reply(self._chat(msgs, max_tokens=320, temperature=temp))
            msgs.append({"role": "assistant", "content": prompt})
            db.update_post(s["id"], prompt=prompt)
            items[i].update(stage="done", prompt=prompt)
            self._set(items=items)
        items[-1]["stage"] = "writing"
        self._set(items=items, detail="mood tags")
        msgs.append({"role": "user", "content": P.MOOD_ASK})
        tags = P.parse_tags(self._chat(msgs, max_tokens=60, temperature=temp)) or ["quiet"]
        items[-1].update(stage="done", prompt=", ".join(tags))
        # only onto the resident that was read: not one deleted meanwhile, or made again under the same handle
        if not db.update_resident(rid, seed=seeds[0]["id"], moods=tags, status="ready"):
            log.info("@%s was deleted while its seed images were read", res["handle"])
            return
        for s in seeds:
            db.update_post(s["id"], tags=json.dumps([slug(t) for t in tags][:4]))
        db.event("resident", f"read @{res['handle']}'s {len(seeds)} seed images → {len(seeds)} base prompts, mood tags: {', '.join(tags)}.", rid)
        self._set(items=items)

    # ---------- the night shift ----------
    def _sources(self, rid: str, before: float) -> list[dict]:
        return [p for p in db.posts("resident_id=? AND created_at<? AND vote>=0 AND snagged_at IS NULL AND image IS NOT NULL", (rid, before), order="id")]

    def context(self, res: dict, rng: random.Random) -> dict:
        """Tonight's examples for one resident: random bites and throw-backs (never snags)."""
        s = config.section("shift")
        n_good, n_bad = max(0, int(s.get("good_examples", 5))), max(0, int(s.get("bad_examples", 3)))
        bites = db.posts("resident_id=? AND kind='edit' AND vote>0 AND snagged_at IS NULL", (res["id"],))
        throws = db.posts("resident_id=? AND kind='edit' AND vote<0 AND snagged_at IS NULL", (res["id"],))
        good = rng.sample(bites, min(len(bites), n_good))
        bad = rng.sample(throws, min(len(throws), n_bad))
        base = [p["prompt"] for p in db.seeds(res["id"]) if p["prompt"]]
        return {"good": good, "bad": bad, "base": base, "n_good": n_good, "n_bad": n_bad,
                "system": P.shift_system(config.section("image"), res, base, [p["prompt"] for p in good], [p["prompt"] for p in bad],
                                         with_bad=n_bad > 0)}

    def write_edit(self, msgs: list[dict], src: dict, near: list[str], rng: random.Random, var: dict) -> tuple[str, list[tuple[str, str]], float]:
        """One edit prompt, in the resident's conversation: tonight's directions, and
        one more try if it comes back too close to its source, a base prompt or
        another prompt from tonight. Returns (prompt, directions, similarity)."""
        directions = P.pick_directions(rng, var)
        msgs.append({"role": "user", "content": P.edit_ask(src["id"], src["prompt"], directions, var)})
        prompt = P.clean_reply(self._chat(msgs, max_tokens=320))
        sim = P.closest(prompt, [src["prompt"], *near])
        if prompt and sim > var["max_similarity"]:
            retry = [*msgs, {"role": "assistant", "content": prompt}, {"role": "user", "content": P.too_close(sim, directions)}]
            again = P.clean_reply(self._chat(retry, max_tokens=320))
            sim2 = P.closest(again, [src["prompt"], *near])
            log.info("edit of #%s came back %.0f%% the same; the second try is %.0f%%", src["id"], sim * 100, sim2 * 100)
            if again and sim2 < sim:
                prompt, sim = again, sim2
        msgs.append({"role": "assistant", "content": prompt})  # the rejected try stays out of the conversation
        return prompt, directions, sim

    def _shift(self, trigger: str) -> None:
        sid = db.start_shift(trigger)
        try:
            self._run_shift(sid, trigger)
        except Exception as exc:
            db.finish_shift(sid, "failed", len(db.posts("shift_id=?", (sid,))), str(exc))
            raise
        finally:
            self._free_image_model()

    def _run_shift(self, sid: int, trigger: str) -> int:
        started = time.time()
        rng = random.Random()
        stems = db.purge_snags()  # snags can be undone until now; their files go here
        for stem in stems:
            media.delete(stem)
        if stems:
            db.event("shift", f"cleared the images of {len(stems)} snagged post{'s' if len(stems) > 1 else ''}.")
        residents = [r for r in db.residents() if r["status"] == "ready"]
        s = config.section("shift")
        n = int(s.get("posts_per_resident", 3))
        var = P.variation(s.get("variation"))
        text_name, image_name = config.section("text")["name"], config.section("image")["name"]
        items: list[dict] = []
        plan: list[tuple[dict, list[dict]]] = []
        for res in residents:
            sources = self._sources(res["id"], started)
            if not sources:
                continue
            # different sources first; repeats only when there are fewer sources than posts
            picks = rng.sample(sources, min(n, len(sources))) + [rng.choice(sources) for _ in range(n - len(sources))]
            plan.append((res, picks))
            for i, src in enumerate(picks):
                items.append({"resident": res["id"], "handle": res["handle"], "source_id": src["id"], "source_image": src["image"],
                              "turn": i + 1, "turns": n, "stage": "queued", "prompt": "", "caption": "", "change": "",
                              "post_id": None, "image": None})
        self._set(state="writing", job="shift", shift_id=sid, phase=f"Phase 1 of 2 · {text_name} is writing prompts", items=items, error=None)
        db.event("shift", f"night shift #{sid} started ({trigger}): {len(items)} edits across {len(plan)} residents, {var['name']} variation.")
        if not items:
            db.finish_shift(sid, "done", 0)
            return 0

        # phase 1 — the language model writes every prompt and caption
        self._free_image_model()
        self._set(detail="loading the language model")
        self.lm().load()
        k = 0
        for res, picks in plan:
            ctx = self.context(res, rng)
            msgs = [{"role": "system", "content": ctx["system"]}]
            written: list[str] = []
            for src in picks:
                it = items[k]
                it["stage"] = "writing"
                self._set(items=items, detail=f"@{res['handle']} · edit {it['turn']} of {it['turns']}")
                prompt, directions, sim = self.write_edit(msgs, src, ctx["base"] + written, rng, var)
                msgs.append({"role": "user", "content": P.CAPTION_ASK})
                caption = (P.clean_reply(self._chat(msgs, max_tokens=48)).splitlines() or [""])[0][:140] if prompt else ""
                msgs.append({"role": "assistant", "content": caption})
                written.append(prompt)
                it.update(stage="written", prompt=prompt, caption=caption.lower(), change=" + ".join(d for d, _ in directions),
                          similarity=round(sim, 2))
                self._set(items=items)
                k += 1
        self.lm().unload()

        # phase 2 — the image model edits every source image
        self._set(state="editing", phase=f"Phase 2 of 2 · {text_name} unloaded · {image_name} is editing images", detail="starting the image model")
        comfy = self.comfy()
        comfy.ensure_running()
        made = 0
        for it in items:
            if not it["prompt"]:
                it["stage"] = "failed"
                continue
            src = db.post(it["source_id"])
            if not src or src["resident_id"] != it["resident"]:  # deleted since tonight's plan (and maybe made again under its handle)
                it.update(stage="failed", error="its resident was deleted")
                continue
            it["stage"] = "editing"
            self._set(items=items, detail=f"@{it['handle']} · edit of #{it['source_id']}")
            try:
                seed = rng.randrange(1, 2**31)
                stem = media.save(comfy.edit(media.png(it["source_image"]), it["prompt"], seed))
                res = db.resident(it["resident"])
                pid = res and db.add_edit(resident_id=it["resident"], kind="edit", source_id=it["source_id"], prompt=it["prompt"],
                                          caption=it["caption"], tags=[slug(t) for t in res["moods"]][:4], image=stem, seed=seed, shift_id=sid)
                if not pid:  # the resident was deleted while it rendered: its files go too
                    media.delete(stem)
                    raise LookupError("its resident was deleted while it rendered")
                it.update(stage="packed", post_id=pid, image=stem)
                made += 1
            except Exception as exc:  # noqa: BLE001
                log.error("edit failed: %s", exc)
                it["stage"] = "failed"
                it["error"] = str(exc)
            self._set(items=items)
        db.finish_shift(sid, "done", made)
        db.event("shift", f"night shift #{sid}: {made} of {len(items)} edits done in {int(time.time() - started)}s.")
        return made

    # ---------- the schedule ----------
    def next_run(self) -> str | None:
        s = config.section("shift")
        return s["schedule_time"] if s.get("schedule_enabled") else None

    @staticmethod
    def due(now: datetime, s: dict) -> datetime | None:
        """The scheduled time a shift is owed for, or None. The last scheduled time
        counts until catch_up_hours after it (a minute when it's 0), so a machine
        that slept through it, or an Angler that wasn't running, still gets the
        night's shift when it's back. Any shift started since then settles it."""
        if not s.get("schedule_enabled"):
            return None
        hh, mm = (int(x) for x in str(s.get("schedule_time", "03:00")).split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at > now:
            at -= timedelta(days=1)
        window = max(60.0, float(s.get("catch_up_hours", 6)) * 3600)
        if (now - at).total_seconds() > window or db.shift_since(at.timestamp()):
            return None
        return at

    def _scheduler(self) -> None:
        while True:
            time.sleep(20)
            try:
                at = self.due(datetime.now(), config.section("shift"))
                if at and self.submit("shift", trigger="schedule")["ok"]:
                    late = int((datetime.now() - at).total_seconds() // 60)
                    if late > 1:
                        db.event("shift", f"caught up on the {at:%H:%M} night shift, {late} minutes late (the PC was asleep or the Angler was off).")
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
