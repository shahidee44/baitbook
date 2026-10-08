"""Every piece of text the language model is given.

The image model's prompt primer goes at the top of everything. The default
primer is for FLUX.2 [klein] (4B and 9B); a different image model brings its own
(`[image] primer_file` in baitbook.toml).
"""

from __future__ import annotations

import difflib
import random
import re

from . import config

KLEIN_PRIMER = """\
# How to write prompts for FLUX.2 [klein]
klein reads prompts with a language-model text encoder, so it understands sentences, not tags. Write the way you would describe a finished photograph or print to someone who cannot see it.
- One paragraph of natural prose, about 30 to 100 words. Longer prompts start to blur.
- Order matters: subject first, then setting and light, then style and finish, then composition and colour.
- Be concrete. Name materials, light direction, time of day, the camera or print process, and specific colours, instead of words like "beautiful" or "aesthetic".
- No keyword lists, no comma-stacked tags, no weight syntax like (word:1.3), no "masterpiece, best quality".
- For emphasis, say it in words: "prominently featuring…", "with particular attention to…".
- Any text in the image goes in quotes, with its font style, colour and placement.
- Describe what is there. Do not list what to avoid inside the prompt.
- For edits (image-to-image), the source image is passed in as a reference. Describe the whole target image: keep what should stay and state what changes."""


def primer(image_cfg: dict) -> str:
    path = config.path(image_cfg.get("primer_file"))
    if path and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return KLEIN_PRIMER


# ---------- the vision read: one conversation per new resident ----------

def read_system(image_cfg: dict, n: int) -> str:
    img = image_cfg["name"]
    return f"""{primer(image_cfg)}

## Your job
You are setting up a new resident of baitbook from their {n} seed images. This conversation has two parts.
1. You will be shown the images one at a time. For each one, write a {img} prompt that would generate that image as closely as possible: the subject, the setting and light, the style and finish, the composition and colours. Each prompt must stand on its own: never refer to the other images. Reply with the prompt only.
2. After the last image you will be asked for mood tags: 3 to 5 lowercase words or short phrases for the mood the images share. Reply with the tags only, separated by commas.

Describe the picture, not what was stamped on it afterwards. Leave out watermarks, signatures, usernames and @handles, website addresses, logos, dates and timestamps: write the prompt as if they were not there. Text that is part of the picture itself (a sign in the scene, a caption designed into the artwork) stays, in quotes."""


def read_ask(image_cfg: dict, i: int, n: int) -> str:
    return (f"[image {i} of {n}]\nWrite the {image_cfg['name']} prompt for this image alone, as if no other image existed "
            "(no \"same as before\", no \"like the first\"). Leave out any watermark, username, signature or date.")


MOOD_ASK = "Looking at all the images together, give 3 to 5 mood tags for this resident."


def parse_tags(text: str) -> list[str]:
    """'Hushed, Sun-warmed , domestic,,' -> ['hushed', 'sun-warmed', 'domestic']"""
    tags = []
    for t in text.replace("\n", ",").split(","):
        t = t.strip().strip(".#*\"'` ").lower()
        if t and t not in tags:
            tags.append(t)
    return tags[:5]


# ---------- the night shift: one conversation per resident ----------

def shift_system(image_cfg: dict, res: dict, base_prompts: list[str], good: list[str], bad: list[str], with_bad: bool = True) -> str:
    """The night's system prompt. with_bad=False ([shift] bad_examples = 0) leaves
    the bad examples out entirely, heading and rule included."""
    first = res["name"].split(" ")[0]
    lines = lambda xs, none: "\n".join(f"- {x}" for x in xs) if xs else none  # noqa: E731
    bad_section = (f"\n\n## Bad examples: {len(bad)} random prompts the viewer threw back (move away from what these have in common)\n"
                   f"{lines(bad, '(none yet)')}") if with_bad else ""
    lean = ("Lean toward what the good examples have in common, and away from what the bad examples have in common."
            if with_bad else "Lean toward what the good examples have in common.")
    return f"""{primer(image_cfg)}

You write image-edit prompts for {res['name']} (@{res['handle']}), a resident of baitbook: a private feed made for one viewer.
Every new image is made by editing one of {first}'s existing images with {image_cfg['name']} (image-to-image).

About {first}: {res['bio']}
Mood tags (from the seed images): {', '.join(res['moods'])}

## Base prompts (from {first}'s seed images; these never change)
{lines(base_prompts, '(none)')}

## Good examples: {len(good)} random prompts the viewer bit (lean toward these)
{lines(good, '(none yet: stay close to the base prompts)')}{bad_section}

## How to answer
You will be given the prompt of one of {first}'s existing images and told what to change. Reply with the prompt for the new image, and nothing else.
- The new image is a new post, not a retouch. It should look like a different picture from the same feed, and the change should be obvious at a glance.
- Keep who and what the picture is about, and its general look, so it still sits comfortably next to the base prompts. Change what you are asked to change, and whatever has to follow from it: a new place brings a new background and new light.
- Write the prompt fresh, in your own words. Don't copy the old prompt's sentences and swap a word or two.
- {lean}
- Leave out watermarks, signatures, usernames and dates, even when the old prompt has them.
- Never write a prompt you have already written in this conversation, and never go back to a base prompt.
- Follow the prompt rules above.
After each prompt you may be asked for a caption. Then reply with one line in {first}'s own voice, as they'd post it: lowercase, no hashtags, no quotes, under 15 words."""


# How far a night's edits move from their source images ([shift] variation).
# `changes` is how many of the DIRECTIONS each edit is given; `max_similarity`
# is how close (0..1, by words) a new prompt may stay to its source, a base
# prompt or another prompt from tonight before it is sent back once.
VARIATION = {
    "subtle": {"changes": 1, "max_similarity": 0.9, "ask": "Keep the change small but clearly visible."},
    "bold": {"changes": 2, "max_similarity": 0.75, "ask": "Make it a clearly different picture."},
    "wild": {"changes": 3, "max_similarity": 0.6, "ask": "Be daring: it can surprise, as long as it is still recognisably theirs."},
}

# What an edit can change: (name, how it's asked, weight). "the medium" is
# only drawn at the wild setting.
DIRECTIONS = [
    ("the place", "the place: move the scene somewhere else that suits them", 3),
    ("the moment", "the moment: a different action, pose or expression", 3),
    ("the time", "the time and weather: another time of day, season or weather, with the light that comes with it", 2),
    ("the outfit", "the outfit: different clothes or accessories that still suit them", 2),
    ("the camera", "the camera: a different shot (closer or wider, another angle, another framing)", 2),
    ("a new detail", "a new detail: bring in an object, food, animal or piece of scenery that becomes part of the picture", 2),
    ("the mood", "the mood: a different feeling, with colours and light to match", 1),
    ("the medium", "the medium: the same subject in another photographic or illustration style", 1),
]


def variation(name: str | None) -> dict:
    return {"name": name if name in VARIATION else "bold", **VARIATION.get(name or "", VARIATION["bold"])}


def pick_directions(rng: random.Random, var: dict) -> list[tuple[str, str]]:
    """`var['changes']` different directions for one edit, drawn by weight."""
    pool = [d for d in DIRECTIONS if var["name"] == "wild" or d[0] != "the medium"]
    picked: list[tuple[str, str]] = []
    while pool and len(picked) < var["changes"]:
        d = rng.choices(pool, weights=[w for _, _, w in pool])[0]
        pool.remove(d)
        picked.append((d[0], d[1]))
    return picked


def edit_ask(source_id: int, source_prompt: str, directions: list[tuple[str, str]], var: dict) -> str:
    change = "\n".join(f"- {how}" for _, how in directions)
    return (f"This is the prompt of image #{source_id}:\n\n{source_prompt}\n\n"
            f"Write the prompt for a new post made by editing this image. Change:\n{change}\n{var['ask']}")


def too_close(similarity: float, directions: list[tuple[str, str]]) -> str:
    what = " and ".join(name for name, _ in directions)
    return (f"That prompt is {round(similarity * 100)}% the same as one you already have. Write it again as a different picture: "
            f"change {what} much more, and use your own words instead of the old sentences. Reply with the prompt only.")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def similarity(a: str, b: str) -> float:
    """How much of two prompts is the same wording, 0..1."""
    return difflib.SequenceMatcher(None, _words(a), _words(b), autojunk=False).ratio()


def closest(prompt: str, others: list[str]) -> float:
    return max((similarity(prompt, o) for o in others if o), default=0.0)


CAPTION_ASK = "Now the caption for that post: one line, in their voice."


def clean_reply(text: str) -> str:
    """Strip the wrapping a chat model sometimes adds around a one-paragraph answer."""
    t = text.strip()
    for tag in ("<think>", "</think>"):
        if tag in t:
            t = t.split("</think>")[-1].strip()
    if t.lower().startswith(("prompt:", "caption:")):
        t = t.split(":", 1)[1].strip()
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'`":
        t = t[1:-1].strip()
    return t.strip("* ").strip()
