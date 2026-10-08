"""angler/config.py: the first run, type-checked updates, comment-keeping writes, paths."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from angler import config


def text(home: Path) -> str:
    return (home / "baitbook.toml").read_text(encoding="utf-8")


# ---------- the first run ----------
def test_first_load_copies_the_example(home):
    assert not config.CONFIG_PATH.exists()
    cfg = config.load()
    assert config.CONFIG_PATH.read_bytes() == config.EXAMPLE_PATH.read_bytes()
    assert cfg == tomllib.loads(config.EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert config.IMAGES.is_dir()


def test_first_load_creates_missing_folders(home, monkeypatch):
    deep = home / "a" / "b" / "baitbook.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", deep)
    monkeypatch.setattr(config, "IMAGES", home / "x" / "y" / "images")
    config.load()
    assert deep.is_file() and (home / "x" / "y" / "images").is_dir()


def test_load_keeps_an_existing_config(home):
    (home / "baitbook.toml").write_text(config.EXAMPLE_PATH.read_text(encoding="utf-8").replace("posts_per_resident = 5", "posts_per_resident = 9"),
                                        encoding="utf-8")
    assert config.load()["shift"]["posts_per_resident"] == 9


def test_example_config_has_every_editable_key():
    example = tomllib.loads(config.EXAMPLE_PATH.read_text(encoding="utf-8"))
    for sec, keys in {**config.EDITABLE}.items():
        for key, typ in keys.items():
            assert key in example[sec], f"[{sec}] {key} is editable but missing from the example config"
            assert isinstance(example[sec][key], typ), f"[{sec}] {key} should be {typ.__name__}"
    for sec, keys in config.LOCAL_ONLY.items():
        for key in keys:
            assert key in example[sec]
    assert example["shift"]["variation"] in config.VARIATIONS


def test_get_returns_a_copy(home):
    c = config.get()
    c["shift"]["posts_per_resident"] = 1234
    assert config.get()["shift"]["posts_per_resident"] != 1234
    assert config.section("shift")["posts_per_resident"] != 1234


# ---------- update ----------
def test_update_converts_types_and_saves(home):
    out = config.update({"text": {"n_ctx": "4096", "temperature": "0.7"}, "shift": {"schedule_enabled": "off", "posts_per_resident": 3.0}})
    assert out["text"]["n_ctx"] == 4096 and out["text"]["temperature"] == 0.7
    assert out["shift"]["schedule_enabled"] is False and out["shift"]["posts_per_resident"] == 3
    on_disk = tomllib.loads(text(home))
    assert on_disk["text"]["n_ctx"] == 4096 and on_disk["text"]["temperature"] == 0.7
    assert on_disk["shift"]["schedule_enabled"] is False and on_disk["shift"]["posts_per_resident"] == 3
    assert config.get() == on_disk


@pytest.mark.parametrize("value, stored", [(True, True), (False, False), ("on", True), ("true", True), ("1", True),
                                           ("yes", True), ("off", False), ("no", False), (0, False), (1, True)])
def test_update_bools(home, value, stored):
    assert config.update({"shift": {"schedule_enabled": value}})["shift"]["schedule_enabled"] is stored


@pytest.mark.parametrize("given, stored", [("3:5", "03:05"), ("23:59", "23:59"), ("07:00", "07:00")])
def test_update_schedule_time_is_normalised(home, given, stored):
    assert config.update({"shift": {"schedule_time": given}})["shift"]["schedule_time"] == stored


@pytest.mark.parametrize("key, given, stored", [("catch_up_hours", 99, 24.0), ("catch_up_hours", -3, 0.0), ("catch_up_hours", "1.5", 1.5),
                                                ("good_examples", 50, 10), ("bad_examples", -1, 0), ("bad_examples", 0, 0)])
def test_update_clamps(home, key, given, stored):
    assert config.update({"shift": {key: given}})["shift"][key] == stored


@pytest.mark.parametrize("changes", [
    {"text": {"n_ctx": "lots"}},
    {"text": {"temperature": "warm"}},
    {"shift": {"variation": "chaotic"}},
    {"shift": {"schedule_time": "noon"}},
    {"shift": {"bogus": 1}},
    {"server": {"port": 1}},  # not editable from the app at all
    {"nonsense": {"x": 1}},
    {"text": {"n_batch": 2}},  # in the file, but not editable
    # the audit's cases: each of these used to be saved, then stopped the schedule or the shift
    {"shift": {"schedule_time": "25:99"}},
    {"shift": {"schedule_time": "-1:30"}},
    {"shift": {"schedule_time": "3"}},
    {"shift": {"posts_per_resident": -1}},
    {"shift": {"posts_per_resident": 0}},
    {"image": {"width": -1}},
    {"image": {"height": 0}},
    {"image": {"steps": 0}},
    {"device": {"keep_nights": -1}},
    {"device": {"keep_nights": 0}},
    # not finite, or not a value at all
    {"image": {"cfg": "nan"}},
    {"text": {"temperature": float("inf")}},
    {"shift": {"catch_up_hours": "nan"}},
    {"text": {"n_ctx": float("inf")}},
    {"text": {"n_ctx": None}},
    {"image": {"width": [1024]}},
    {"shift": ["posts_per_resident", 3]},
])
def test_update_rejects(home, changes):
    config.load()
    before, cfg = text(home), config.get()
    with pytest.raises(ValueError):
        config.update(changes)
    assert text(home) == before
    assert config.get() == cfg


@pytest.mark.parametrize("sec, key, value", [
    ("shift", "schedule_time", "00:00"), ("shift", "schedule_time", "23:59"), ("shift", "posts_per_resident", 1),
    ("image", "width", 64), ("image", "height", 4096), ("image", "steps", 1), ("image", "cfg", 0.0),
    ("text", "temperature", 0.0), ("device", "keep_nights", 1),
])
def test_update_takes_the_ends_of_each_range(home, sec, key, value):
    assert config.update({sec: {key: value}})[sec][key] == value


def test_the_example_config_is_in_range(home):
    example = tomllib.loads(config.EXAMPLE_PATH.read_text(encoding="utf-8"))
    for (sec, key), (lo, hi) in {**config.LIMITS, **config.CLAMPS}.items():
        assert lo <= example[sec][key] <= hi, (sec, key)


def test_update_rejects_local_only_keys_without_local(home):
    config.load()
    before = text(home)
    for key in ("comfy_dir", "comfy_python"):
        with pytest.raises(ValueError, match="can't be changed from the app"):
            config.update({"image": {key: "C:/evil"}})
    assert text(home) == before


def test_update_local_allows_local_only_keys(home):
    out = config.update({"image": {"comfy_dir": "C:/ComfyUI", "comfy_python": "venv/python.exe", "steps": 6}}, local=True)
    assert out["image"]["comfy_dir"] == "C:/ComfyUI" and out["image"]["comfy_python"] == "venv/python.exe"
    on_disk = tomllib.loads(text(home))["image"]
    assert on_disk["comfy_dir"] == "C:/ComfyUI" and on_disk["comfy_python"] == "venv/python.exe" and on_disk["steps"] == 6


def test_update_local_still_rejects_unknown_keys(home):
    with pytest.raises(ValueError):
        config.update({"image": {"rm_rf": "/"}}, local=True)
    with pytest.raises(ValueError):
        config.update({"server": {"host": "0.0.0.0"}}, local=True)


def test_update_is_all_or_nothing(home):
    config.load()
    before = text(home)
    with pytest.raises(ValueError):
        config.update({"shift": {"good_examples": 2, "variation": "chaotic"}})
    assert config.get()["shift"]["good_examples"] == 5
    assert text(home) == before


# ---------- _write_value: comments stay ----------
def test_write_keeps_comments_and_the_comment_column(home):
    config.load()
    before = text(home).splitlines()
    config.update({"shift": {"good_examples": 7, "variation": "subtle", "schedule_time": "4:30"}})
    after = text(home).splitlines()
    assert len(after) == len(before)
    changed = {i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b}
    assert len(changed) == 3, "only the three changed lines differ"
    for i in changed:
        old, new = before[i], after[i]
        assert old.index("#") == new.index("#"), f"comment column moved:\n{old}\n{new}"
        assert old[old.index("#"):] == new[new.index("#"):]
    assert any(line.startswith("good_examples = 7 ") for line in after)
    assert any(line.startswith('variation = "subtle" ') for line in after)
    assert any(line.startswith('schedule_time = "04:30" ') for line in after)


def test_write_a_value_longer_than_the_gap_keeps_its_comment(home):
    config.load()
    long = "a model with a very long name that will not fit before the comment column at all"
    config.update({"image": {"primer_file": long}})
    line = next(line for line in text(home).splitlines() if line.startswith("primer_file"))
    assert line.startswith(f'primer_file = "{long}" #')
    assert line.endswith("# empty = the built-in FLUX.2 [klein] primer")
    assert config.load()["image"]["primer_file"] == long


def test_write_only_touches_its_own_section(home):
    """`name` is in [text] and in [image]."""
    cfg = config.load()
    config.update({"image": {"name": "Other Model"}})
    cfg2 = tomllib.loads(text(home))
    assert cfg2["image"]["name"] == "Other Model"
    assert cfg2["text"]["name"] == cfg["text"]["name"]


@pytest.mark.parametrize("value", ['say "cheese" # not a comment', "back\\slash", "ünïcødé ✓", "tab\there", "", "#"])
def test_write_strings_round_trip(home, value):
    config.update({"text": {"name": value}})
    assert tomllib.loads(text(home))["text"]["name"] == value
    assert config.load()["text"]["name"] == value


def test_write_string_with_hash_keeps_the_comment(home):
    config.update({"image": {"primer_file": "notes#1.md"}})
    config.update({"image": {"primer_file": "notes#2.md"}})  # the old value has a # in it too
    line = next(line for line in text(home).splitlines() if line.startswith("primer_file"))
    assert line.startswith('primer_file = "notes#2.md"') and line.endswith("# empty = the built-in FLUX.2 [klein] primer")


SHORT_CONFIG = ("# my own config\n[server]\nport = 8787\n\n[text]\nname = \"x\"\n[image]\nname = \"y\"\n"
                "[shift]\nvariation = \"bold\"   # how far\n")


def test_write_adds_a_missing_key(home):
    (home / "baitbook.toml").write_text(SHORT_CONFIG, encoding="utf-8")
    config.load()
    config.update({"shift": {"good_examples": 2}})
    t = text(home)
    assert t.startswith("# my own config\n")
    assert 'variation = "bold"   # how far' in t
    assert tomllib.loads(t)["shift"] == {"good_examples": 2, "variation": "bold"}
    shift = config.get()["shift"]
    assert shift["good_examples"] == 2 and shift["variation"] == "bold"
    assert shift["posts_per_resident"] == 5  # left out of the file: the example's value, in memory only


def test_missing_sections_and_keys_take_the_examples_defaults(home):
    (home / "baitbook.toml").write_text(SHORT_CONFIG, encoding="utf-8")
    c = config.load()
    assert c["device"]["keep_nights"] == 7 and c["server"]["port"] == 8787 and c["text"]["name"] == "x"
    assert "[device]" not in text(home)  # the file itself is left alone


def test_write_adds_a_missing_section(home):
    (home / "baitbook.toml").write_text(SHORT_CONFIG, encoding="utf-8")
    config.load()
    config.update({"device": {"keep_nights": 3}})
    assert tomllib.loads(text(home))["device"] == {"keep_nights": 3}


def test_write_ignores_commented_out_keys(home):
    (home / "baitbook.toml").write_text("[shift]\n# good_examples = 1\ngood_examples = 5  # real\n", encoding="utf-8")
    config.load()
    config.update({"shift": {"good_examples": 3}})
    assert text(home) == "[shift]\n# good_examples = 1\ngood_examples = 3  # real\n"


def test_write_section_header_with_a_comment(home):
    (home / "baitbook.toml").write_text("[shift]   # the night shift\ngood_examples = 5\n", encoding="utf-8")
    config.load()
    config.update({"shift": {"good_examples": 3}})
    assert tomllib.loads(text(home))["shift"]["good_examples"] == 3


# ---------- path / local_port ----------
def test_path(home):
    assert config.path("") is None
    assert config.path(None) is None
    assert config.path("models/gemma.gguf") == home / "models" / "gemma.gguf"
    absolute = (home / "elsewhere" / "x.gguf").resolve()
    assert config.path(str(absolute)) == absolute
    assert config.path("~/m.gguf") == Path.home() / "m.gguf"


def test_local_port():
    assert config.local_port({"port": 8787, "ssl_certfile": ""}) == 8787
    assert config.local_port({"port": "9000"}) == 9000
    assert config.local_port({"port": 8787, "ssl_certfile": "cert.pem", "local_port": 9999}) == 9999
    assert config.local_port({"port": 8787, "ssl_certfile": "cert.pem"}) == 8788
    assert config.local_port({"port": 8787, "ssl_certfile": "cert.pem", "local_port": 0}) == 8788


def test_angler_port_follows_a_running_anglers_note(home):
    """`baitbook angler --port 9000` notes 9000 in data/angler.port, and holds the data folder."""
    from angler.__main__ import lock_data

    server = config.load()["server"]
    note = config.DATA / "angler.port"
    assert config.angler_port(server) == 8787  # no note
    note.write_text("9000", encoding="ascii")
    assert config.angler_port(server) == 8787  # a note, but no Angler: one that was killed left it
    held = lock_data()
    try:
        assert config.angler_port(server) == 9000
        note.write_text("not a port", encoding="ascii")
        assert config.angler_port(server) == 8787
        note.write_text("8787", encoding="ascii")
        assert config.angler_port(server) == 8787
    finally:
        held.close()


def test_a_byte_order_mark_is_fine(home):
    # Windows PowerShell 5.1 (Set-Content -Encoding utf8) and older Notepad write one
    (home / "baitbook.toml").write_bytes(b"\xef\xbb\xbf" + SHORT_CONFIG.encode())
    assert config.load()["server"]["port"] == 8787
    config.update({"shift": {"good_examples": 1}})
    assert tomllib.loads(text(home).lstrip("﻿"))["shift"]["good_examples"] == 1


def test_override_is_kept_over_every_load_and_never_written(home):
    """`baitbook angler --port 9000` holds for that process: whatever reads [server] sees 9000."""
    config.load()
    before = (home / "baitbook.toml").read_text(encoding="utf-8")
    cfg = config.override({"server": {"port": 9000, "host": "127.0.0.1"}})
    assert cfg["server"]["port"] == 9000 and cfg["server"]["host"] == "127.0.0.1"
    assert config.load()["server"]["port"] == 9000  # the server's startup loads the file again
    assert config.section("server")["port"] == 9000 and config.section("server")["ssl_certfile"] == ""
    config.update({"device": {"keep_nights": 3}})  # a save from the PC app writes the file, not the override
    assert config.section("server")["port"] == 9000
    after = (home / "baitbook.toml").read_text(encoding="utf-8")
    assert tomllib.loads(after)["server"] == tomllib.loads(before)["server"]
