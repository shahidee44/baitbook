"""baitbook/models.py: downloading the default models. Nothing comes from Hugging Face:
a fake hub on a free loopback port serves small stand-ins for the five files (the same
names, made-up bytes) and answers Range requests, and the module's table points at it."""

from __future__ import annotations

import hashlib
import io
import random
import re
import sys
import threading
import tomllib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from angler import config
from baitbook import models


class Hub(BaseHTTPRequestHandler):
    """Hugging Face's /<repo>/resolve/<revision>/<file>, with Range, and two ways to misbehave:
    ignore_range (a 200 with the whole file) and cut_after (the connection drops partway, once)."""

    state: SimpleNamespace

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        st = self.state
        asked = self.headers.get("Range")
        st.requests.append((self.path, asked))
        data = st.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        if asked and not st.ignore_range:
            start = int(asked.removeprefix("bytes=").split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if st.cut_after is not None:
            body, st.cut_after = body[:st.cut_after], None
        self.wfile.write(body)


def url_path(item) -> str:
    return f"/{item.repo}/resolve/{item.revision}/{item.file}"


@pytest.fixture
def hub(monkeypatch):
    """The fake hub, and the module pointed at it: .item / .data by setting, .requests as (path, Range)."""
    files, table = {}, []
    for n, item in enumerate(models.DEFAULTS):
        data = random.Random(item.name).randbytes(30_000 + 7_919 * n)
        table.append(replace(item, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        files[url_path(item)] = data
    Hub.state = st = SimpleNamespace(files=files, requests=[], ignore_range=False, cut_after=None)
    st.item = {i.key: i for i in table}
    st.data = {i.key: files[url_path(i)] for i in table}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    monkeypatch.setattr(models, "HF", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(models, "DEFAULTS", tuple(table))
    monkeypatch.setattr(models, "CHUNK", 4096)  # several chunks per file
    monkeypatch.setattr(models, "BACKOFF", 0)  # retry at once
    try:
        yield st
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def comfy(home):
    """ComfyUI as `baitbook comfyui` sets it up: its models under <home>/comfyui/models."""
    config.load()
    config.update({"image": {"comfy_base": str(home / "comfyui")}}, local=True)
    return home / "comfyui" / "models"


@pytest.fixture
def no_questions(monkeypatch):
    def ask(*a, **k):
        raise AssertionError(f"asked a question: {a}")

    monkeypatch.setattr("builtins.input", ask)


def keys(items) -> list[str]:
    return [i.key for i in items]


def saved(home) -> dict:
    return tomllib.loads((home / "baitbook.toml").read_text(encoding="utf-8"))


# ---------- the table ----------
def test_every_default_is_pinned():
    for item in models.DEFAULTS:
        assert re.fullmatch(r"[0-9a-f]{40}", item.revision) and re.fullmatch(r"[0-9a-f]{64}", item.sha256)
        assert item.size > 10**8
        assert item.url == f"https://huggingface.co/{item.repo}/resolve/{item.revision}/{item.file}"
    image = config.load()["image"]  # the example config names the image defaults
    assert {i.key: i.name for i in models.DEFAULTS if i.section == "image"} == {k: image[k] for k in ("unet", "text_encoder", "vae")}


# ---------- what counts as missing ----------
def test_a_fresh_config_misses_everything():
    config.load()  # [text] empty; [image] the default names, and no ComfyUI to find them in
    assert keys(models.missing()) == ["model", "mmproj", "unet", "text_encoder", "vae"]


def test_whats_there_isnt_missing(home, comfy):
    gguf = home / "elsewhere" / "gemma-4-31B_q4_0-it.gguf"
    gguf.parent.mkdir()
    gguf.write_bytes(b"GGUF")
    config.update({"text": {"model": str(gguf), "mmproj": "gemma-4-31B-it-mmproj.gguf"}})  # mmproj: relative, not there
    for rel in ("unet/flux-2-klein-4b.safetensors", "vae/flux2/flux2-vae.safetensors"):  # ComfyUI's older folder; a subfolder
        (comfy / rel).parent.mkdir(parents=True)
        (comfy / rel).write_bytes(b"x")
    assert keys(models.missing()) == ["mmproj", "text_encoder"]
    (home / "gemma-4-31B-it-mmproj.gguf").write_bytes(b"GGUF")  # relative to baitbook.toml
    assert keys(models.missing()) == ["text_encoder"]


def test_the_users_own_choice_is_left_alone(home, comfy, hub, capsys, no_questions):
    own = str(home / "nowhere" / "my-gemma-Q8_0.gguf")
    config.update({"text": {"model": own}, "image": {"unet": "flux-2-klein-9b-Q8_0.gguf", "vae": "ae.safetensors"}})
    assert keys(models.missing()) == ["mmproj", "text_encoder"]  # the others aren't there either, but aren't ours to fetch
    assert models.run(yes=True) == 0
    assert "(your own choice, left alone)" in capsys.readouterr().out
    assert sorted(p.rsplit("/", 1)[1] for p, _ in hub.requests) == ["gemma-4-31B-it-mmproj.gguf", "qwen_3_4b.safetensors"]
    s = saved(home)
    assert (s["text"]["model"], s["image"]["unet"], s["image"]["vae"]) == (own, "flux-2-klein-9b-Q8_0.gguf", "ae.safetensors")


def test_without_comfyui_the_image_files_wait(home, hub, capsys, no_questions):
    config.load()
    assert models.run(yes=True) == 1
    out = capsys.readouterr().out
    assert "set up ComfyUI first (`baitbook comfyui`)" in out and "3 still missing" in out
    assert sorted(p.rsplit("/", 1)[1] for p, _ in hub.requests) == ["gemma-4-31B-it-mmproj.gguf", "gemma-4-31B_q4_0-it.gguf"]
    assert keys(models.missing()) == ["unet", "text_encoder", "vae"]
    assert saved(home)["text"]["model"] == str(home / "models" / "gemma-4-31B_q4_0-it.gguf")


# ---------- downloading ----------
def test_a_full_download_sets_the_config(home, comfy, hub, capsys, no_questions):
    config.update({"image": {"vae": ""}})  # empty: the default, by name
    assert models.run(yes=True) == 0
    for key, item in hub.item.items():
        path = home / "models" / item.name if item.section == "text" else comfy / item.folder / item.name
        assert path.read_bytes() == hub.data[key]
        assert not path.with_name(f"{item.name}.part").exists()
    s = saved(home)
    assert s["text"]["model"] == str(home / "models" / "gemma-4-31B_q4_0-it.gguf")
    assert s["text"]["mmproj"] == str(home / "models" / "gemma-4-31B-it-mmproj.gguf")
    assert [s["image"][k] for k in ("unet", "text_encoder", "vae")] == ["flux-2-klein-4b.safetensors", "qwen_3_4b.safetensors", "flux2-vae.safetensors"]
    assert len(hub.requests) == 5 and all(rng is None for _, rng in hub.requests)
    out = capsys.readouterr().out
    assert f"{models.gb(sum(i.size for i in hub.item.values()))} in all" in out
    assert "  > flux2-vae.safetensors  0.00 / 0.00 GB" in out  # plain progress lines, with no terminal
    assert out.rstrip().endswith("All the default models are in place.")

    assert models.missing() == [] and models.run(yes=True) == 0
    assert len(hub.requests) == 5  # nothing fetched again


def test_a_file_already_downloaded_is_only_pointed_at(home, comfy, hub, no_questions):
    (home / "models").mkdir()
    (home / "models" / "gemma-4-31B-it-mmproj.gguf").write_bytes(hub.data["mmproj"])
    assert "mmproj" in keys(models.missing())  # the setting is empty
    assert models.run(yes=True) == 0
    assert url_path(hub.item["mmproj"]) not in [p for p, _ in hub.requests]
    assert saved(home)["text"]["mmproj"] == str(home / "models" / "gemma-4-31B-it-mmproj.gguf")


def test_resume_after_an_interrupted_part(home, comfy, hub, capsys, no_questions):
    item, data = hub.item["vae"], hub.data["vae"]
    part = comfy / "vae" / "flux2-vae.safetensors.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:10_000])
    assert models.run(yes=True) == 0
    assert (comfy / "vae" / "flux2-vae.safetensors").read_bytes() == data and not part.exists()
    assert [rng for p, rng in hub.requests if p == url_path(item)] == ["bytes=10000-"]
    out = capsys.readouterr().out
    assert "already here" in out and "  > checking flux2-vae.safetensors" in out


def test_a_dropped_connection_picks_up_where_it_stopped(home, comfy, hub, capsys, no_questions):
    hub.cut_after = 10_000  # the first file's connection drops partway
    assert models.run(yes=True) == 0
    item = hub.item["model"]
    assert [rng for p, rng in hub.requests if p == url_path(item)] == [None, "bytes=10000-"]
    assert (home / "models" / item.name).read_bytes() == hub.data["model"]
    assert "the connection closed early; trying again" in capsys.readouterr().out


def test_a_200_for_a_range_starts_over(home, comfy, hub, capsys, no_questions):
    part = home / "models" / "gemma-4-31B-it-mmproj.gguf.part"
    part.parent.mkdir()
    part.write_bytes(b"not the start of the file " * 100)  # kept, it would spoil the sha256
    hub.ignore_range = True
    assert models.run(yes=True) == 0
    assert (home / "models" / "gemma-4-31B-it-mmproj.gguf").read_bytes() == hub.data["mmproj"]
    assert (url_path(hub.item["mmproj"]), "bytes=2600-") in hub.requests
    assert "can't resume it; starting again" in capsys.readouterr().out


def test_a_sha256_mismatch_deletes_the_part(home, comfy, hub, monkeypatch, capsys, no_questions):
    monkeypatch.setattr(models, "DEFAULTS", tuple(replace(i, sha256="0" * 64) if i.key == "vae" else i for i in models.DEFAULTS))
    config.update({"image": {"vae": ""}})
    assert models.run(yes=True) == 1
    out = capsys.readouterr().out
    assert "flux2-vae.safetensors didn't download correctly (its sha256 doesn't match" in out and "1 still missing" in out
    assert not list((comfy / "vae").iterdir())  # neither the file nor its .part
    assert saved(home)["image"]["vae"] == ""  # never pointed at a file that isn't there
    assert keys(models.missing()) == ["vae"]  # the rest went on and downloaded


def test_a_missing_file_fails_clearly(home, comfy, hub, capsys, no_questions):
    del hub.files[url_path(hub.item["unet"])]
    assert models.run(yes=True) == 1
    assert "flux-2-klein-4b.safetensors: 404" in capsys.readouterr().out
    assert keys(models.missing()) == ["unet"]


def test_not_enough_disk(home, comfy, hub, monkeypatch, capsys, no_questions):
    free = [models.MARGIN + sum(i.size for i in hub.item.values()) - 1]  # one byte short
    monkeypatch.setattr(models.shutil, "disk_usage", lambda path: SimpleNamespace(total=10**12, used=0, free=free[0]))
    assert models.run(yes=True) == 1
    assert "not enough disk space" in capsys.readouterr().out
    assert hub.requests == [] and not (home / "models").exists() and not comfy.exists()
    free[0] += 1  # just enough
    assert models.run(yes=True) == 0


def test_ctrl_c_keeps_the_part(home, comfy, hub, monkeypatch, capsys, no_questions):
    real = models.Progress.update

    def update(self, done):
        real(self, done)
        if self.label == "gemma-4-31B_q4_0-it.gguf" and done >= 8192:
            raise KeyboardInterrupt

    monkeypatch.setattr(models.Progress, "update", update)
    assert models.run(yes=True) == 130
    part = home / "models" / "gemma-4-31B_q4_0-it.gguf.part"
    assert part.stat().st_size == 8192
    assert "`baitbook models` picks up from there" in capsys.readouterr().out
    monkeypatch.setattr(models.Progress, "update", real)
    assert models.run(yes=True) == 0
    assert (url_path(hub.item["model"]), "bytes=8192-") in hub.requests
    assert (home / "models" / "gemma-4-31B_q4_0-it.gguf").read_bytes() == hub.data["model"]


# ---------- run() and offer() ----------
def test_run_asks_unless_yes(home, comfy, hub, monkeypatch, capsys):
    answers = []
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.append(prompt) or "n")
    assert models.run() == 1 and hub.requests == []
    assert answers == ["      download them now? [Y/n]: "]
    assert "Nothing downloaded" in capsys.readouterr().out

    def no_terminal(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)  # nothing to answer from: no
    assert models.run() == 1 and hub.requests == []
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # Enter: yes
    assert models.run() == 0 and len(hub.requests) == 5


def test_offer_without_asking_asks_nothing(home, comfy, hub, capsys, no_questions):
    assert models.offer(ask=False) == 0
    assert models.missing() == [] and len(hub.requests) == 5
    capsys.readouterr()
    assert models.offer(ask=False) == 0
    assert capsys.readouterr().out == ""  # nothing to say when nothing's missing


def test_offer_without_comfyui(home, hub, capsys, no_questions):
    config.load()
    assert models.offer(ask=False) == 3  # the image files wait for ComfyUI
    assert "set up ComfyUI first" in capsys.readouterr().out


def test_offer_asks_first(home, comfy, hub, monkeypatch, capsys):
    answers, asked = iter(["n", ""]), []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or next(answers))
    assert models.offer(ask=True) == 5 and hub.requests == []
    out = capsys.readouterr().out
    assert "default models to download from Hugging Face" in out and "flux-2-klein-4b.safetensors" in out
    assert models.offer(ask=True) == 0 and len(hub.requests) == 5
    assert asked == ["      download them now? [Y/n]: "] * 2


# ---------- progress ----------
def test_progress_on_a_terminal_is_one_line(monkeypatch):
    out = io.StringIO()
    out.isatty = lambda: True
    now = [0.0]
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: now[0]))
    with models.Progress("x.gguf", 2 * 10**9) as p:
        now[0] = 10.0
        p.update(10**9)
    text = out.getvalue()
    assert text.startswith("\r  > x.gguf  0.00 / 2.00 GB")
    assert "\r  > x.gguf  1.00 / 2.00 GB  100.0 MB/s  ETA 0:10" in text
    assert text.count("\n") == 1 and text.endswith("\n")


def test_progress_without_a_terminal_prints_a_line_every_10_percent(capsys):
    with models.Progress("x.gguf", 10 * 10**9) as p:
        for done in range(0, 10 * 10**9 + 1, 10**8):
            p.update(done)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 11 and lines[0].startswith("  > x.gguf  0.00 / 10.00 GB")
    assert lines[-1].startswith("  > x.gguf  10.00 / 10.00 GB")
