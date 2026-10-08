"""angler/prompts.py: every piece of text the language model is given."""

from __future__ import annotations

import random

import pytest

from angler import config
from angler import prompts as P

IMAGE_CFG = {"name": "FLUX.2 [klein] 4B", "primer_file": ""}
RES = {"name": "Moss Finch", "handle": "moss", "bio": "lives by the sea", "moods": ["hushed", "salt"]}


# ---------- parse_tags ----------
@pytest.mark.parametrize("text, tags", [
    ("Hushed, Sun-warmed , domestic,,", ["hushed", "sun-warmed", "domestic"]),
    ("hushed\nsalt-stained\n\nlate", ["hushed", "salt-stained", "late"]),
    ("**Cozy**, #rainy, \"quiet\", `slow`.", ["cozy", "rainy", "quiet", "slow"]),
    ("calm, Calm, CALM, calm.", ["calm"]),
    ("a, b, c, d, e, f, g", ["a", "b", "c", "d", "e"]),
    ("", []),
    (" , ,\n", []),
])
def test_parse_tags(text, tags):
    assert P.parse_tags(text) == tags


# ---------- clean_reply ----------
@pytest.mark.parametrize("text, clean", [
    ("a fox in the snow", "a fox in the snow"),
    ("  a fox in the snow \n", "a fox in the snow"),
    ("Prompt: a fox in the snow", "a fox in the snow"),
    ("caption: out by the water", "out by the water"),
    ('Prompt: "a fox in the snow"', "a fox in the snow"),
    ('"a fox in the snow"', "a fox in the snow"),
    ("'a fox'", "a fox"),
    ("`a fox`", "a fox"),
    ("**a fox in the snow**", "a fox in the snow"),
    ("<think>let me plan this</think>\nA fox in the snow", "A fox in the snow"),
    ("<think>plan</think>Prompt: \"a fox\"", "a fox"),
    ('"', '"'),  # a lone quote is not a wrapping pair
    ("", ""),
])
def test_clean_reply(text, clean):
    assert P.clean_reply(text) == clean


def test_clean_reply_keeps_inner_quotes_and_colons():
    assert P.clean_reply('a sign reading "OPEN": neon, at night') == 'a sign reading "OPEN": neon, at night'


# ---------- similarity / closest ----------
def test_similarity_bounds_and_normalisation():
    assert P.similarity("a red fox in snow", "a red fox in snow") == 1.0
    assert P.similarity("A red FOX, in snow!", "a red fox in snow") == 1.0  # case and punctuation don't count
    assert P.similarity("a red fox in snow", "zebra quartz orchard lantern") == 0.0
    assert 0.0 <= P.similarity("", "") <= 1.0  # defined, no crash
    mid = P.similarity("a red fox sitting in deep snow", "a red fox sitting on a sunny beach")
    assert 0.3 < mid < 0.9


def test_similarity_is_by_words_not_characters():
    # "cat" and "cart" share most characters but no words
    assert P.similarity("cat", "cart") == 0.0


def test_closest():
    others = ["a red fox in snow", "", "zebra quartz", "a red fox on a beach"]
    assert P.closest("a red fox in snow", others) == 1.0
    assert P.closest("zebra quartz orchard", ["", "a red fox"]) == 0.0
    assert P.closest("anything", []) == 0.0
    assert P.closest("anything", ["", ""]) == 0.0
    assert P.closest("a red fox on a beach at noon", others) == max(P.similarity("a red fox on a beach at noon", o) for o in others if o)


# ---------- variation / pick_directions ----------
@pytest.mark.parametrize("name, expected", [("subtle", "subtle"), ("bold", "bold"), ("wild", "wild"),
                                            (None, "bold"), ("", "bold"), ("nope", "bold"), ("WILD", "bold")])
def test_variation(name, expected):
    v = P.variation(name)
    assert v["name"] == expected
    assert {k: v[k] for k in ("changes", "max_similarity", "ask")} == P.VARIATION[expected]


def test_variation_returns_a_copy():
    v = P.variation("wild")
    v["changes"] = 99
    assert P.VARIATION["wild"]["changes"] == 3


@pytest.mark.parametrize("name", ["subtle", "bold", "wild"])
def test_pick_directions_count_and_distinct(name):
    var = P.variation(name)
    names = {d[0] for d in P.DIRECTIONS}
    for seed in range(300):
        picked = P.pick_directions(random.Random(seed), var)
        assert len(picked) == var["changes"]
        assert len({p[0] for p in picked}) == len(picked), "a direction was picked twice"
        assert all(p[0] in names and isinstance(p[1], str) and p[1] for p in picked)


@pytest.mark.parametrize("name", ["subtle", "bold"])
def test_pick_directions_never_the_medium_unless_wild(name):
    var = P.variation(name)
    for seed in range(2000):
        assert "the medium" not in {p[0] for p in P.pick_directions(random.Random(seed), var)}


def test_pick_directions_wild_can_change_the_medium():
    var = P.variation("wild")
    assert any("the medium" in {p[0] for p in P.pick_directions(random.Random(seed), var)} for seed in range(500))


def test_pick_directions_is_reproducible_and_does_not_touch_directions():
    before = list(P.DIRECTIONS)
    a = P.pick_directions(random.Random(42), P.variation("wild"))
    b = P.pick_directions(random.Random(42), P.variation("wild"))
    assert a == b
    assert P.DIRECTIONS == before


def test_pick_directions_stops_when_the_pool_runs_out():
    var = {"name": "bold", "changes": 50, "max_similarity": 0.5, "ask": ""}
    picked = P.pick_directions(random.Random(1), var)
    assert len(picked) == len(P.DIRECTIONS) - 1  # everything but the medium


# ---------- the system prompts ----------
def test_shift_system_with_bad_examples():
    s = P.shift_system(IMAGE_CFG, RES, ["base one", "base two"], ["good one"], ["bad one", "bad two"])
    assert s.startswith(P.KLEIN_PRIMER)
    assert "Moss Finch (@moss)" in s and "About Moss: lives by the sea" in s
    assert "Mood tags (from the seed images): hushed, salt" in s
    assert "- base one\n- base two" in s
    assert "## Good examples: 1 random prompts" in s and "- good one" in s
    assert "## Bad examples: 2 random prompts" in s and "- bad one\n- bad two" in s
    assert "away from what the bad examples have in common" in s
    assert "FLUX.2 [klein] 4B" in s


def test_shift_system_with_bad_on_but_none_yet():
    s = P.shift_system(IMAGE_CFG, RES, ["base"], [], [])
    assert "## Bad examples: 0 random prompts" in s and "(none yet)" in s
    assert "(none yet: stay close to the base prompts)" in s


def test_shift_system_without_bad_examples():
    s = P.shift_system(IMAGE_CFG, RES, [], ["good one"], ["bad one"], with_bad=False)
    assert "Bad examples" not in s
    assert "bad one" not in s
    assert "bad examples" not in s.lower()
    assert "Lean toward what the good examples have in common." in s
    assert "- (none)" not in s and "(none)" in s  # no base prompts yet


def test_primer_file_relative_to_the_config(home):
    config.load()
    (home / "my-primer.md").write_text("  # My own primer\nwrite haiku  \n", encoding="utf-8")
    cfg = {**IMAGE_CFG, "primer_file": "my-primer.md"}
    assert P.primer(cfg) == "# My own primer\nwrite haiku"
    assert P.shift_system(cfg, RES, [], [], []).startswith("# My own primer")
    assert P.read_system(cfg, 5).startswith("# My own primer")


def test_primer_falls_back_when_the_file_is_missing(home):
    assert P.primer({**IMAGE_CFG, "primer_file": "nope.md"}) == P.KLEIN_PRIMER
    assert P.primer({**IMAGE_CFG, "primer_file": ""}) == P.KLEIN_PRIMER
    assert P.primer({"name": "x"}) == P.KLEIN_PRIMER


def test_read_prompts():
    s = P.read_system(IMAGE_CFG, 5)
    assert "from their 5 seed images" in s and "watermarks" in s
    ask = P.read_ask(IMAGE_CFG, 2, 5)
    assert ask.startswith("[image 2 of 5]") and "FLUX.2 [klein] 4B prompt" in ask


def test_edit_ask_and_too_close():
    dirs = [("the place", "the place: move it"), ("the camera", "the camera: closer")]
    ask = P.edit_ask(17, "a fox in snow", dirs, P.variation("bold"))
    assert "image #17" in ask and "a fox in snow" in ask
    assert "- the place: move it\n- the camera: closer" in ask
    assert ask.endswith(P.VARIATION["bold"]["ask"])
    msg = P.too_close(0.876, dirs)
    assert "88% the same" in msg and "change the place and the camera much more" in msg
