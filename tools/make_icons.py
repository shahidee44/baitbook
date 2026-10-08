"""Draw the baitbook icon (web/device/icon.svg) at every size the apps need.

    .venv\\Scripts\\python.exe tools\\make_icons.py

Pillow can't read SVG, so the shapes below repeat icon.svg's numbers (a 512 x 512 viewBox):
a cream rounded square, and a brown hook: a 30-wide stroke with round caps (a straight shank,
then three quarters of a circle up to the point) and a dot for the eye. Each image is drawn
at several times its size and scaled down, which gives smooth edges.

The files it writes are committed, so installing baitbook never needs Pillow for them:
  baitbook/icons/baitbook.png    512, for the Linux app menu
  baitbook/icons/baitbook.ico    16-256, for the Windows shortcut
  baitbook/icons/baitbook.icns   32-1024, for the macOS app bundle (drawn on Apple's icon grid)
  web/device/icon-192.png, icon-512.png   the phone app's icons
  web/device/icon-maskable-512.png        full-bleed, the hook inside the maskable safe zone
  web/device/apple-touch-icon.png         180, full-bleed with no transparency (iOS rounds it)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "baitbook" / "icons"
DEVICE = ROOT / "web" / "device"

CREAM = (0xF4, 0xED, 0xE1, 255)
BROWN = (0x7A, 0x60, 0x48, 255)

# icon.svg, in its 512 viewBox
BOX = 512
CORNER = 112                      # <rect width="512" height="512" rx="112">
HALF = 30 / 2                     # stroke-width="30", stroke-linecap="round"
SHANK_TOP, SHANK_BOTTOM = (256, 96), (256, 272)  # M256 96 v176
# a72 72 0 1 1 -72 -72: from (256,272) to (184,200), radius 72, the large arc, clockwise.
# Of the two circles through both points, that's the one centred at (184,272): it leaves
# (256,272) at 0 degrees (3 o'clock), passes under and round the left, and ends at 270 (the point).
BEND, BEND_R = (184, 272), 72
POINT = (184, 200)
EYE, EYE_R = (256, 100), 26       # <circle cx="256" cy="100" r="26">

MAC_GRID = 824 / 1024             # macOS icons: the rounded square fills 824 of a 1024 canvas


def render(size: int, *, fill: float = 1.0, full_bleed: bool = False) -> Image.Image:
    """The icon at size x size (RGBA). fill: the share of the canvas the 512 box takes, centred.
    full_bleed: cream to the edges, no rounded corners (for icons the platform masks itself)."""
    ss = max(2, min(8, 4096 // size))  # supersampling: about 4096 px across while drawing
    s = size * ss
    k = s * fill / BOX
    off = s * (1 - fill) / 2

    def xy(x: float, y: float) -> tuple[float, float]:
        return off + x * k, off + y * k

    def disc(c: tuple[float, float], r: float) -> None:
        cx, cy = xy(*c)
        draw.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=BROWN)

    img = Image.new("RGBA", (s, s), CREAM if full_bleed else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if not full_bleed:
        draw.rounded_rectangle([*xy(0, 0), *(v - 1 for v in xy(BOX, BOX))], radius=CORNER * k, fill=CREAM)
    # the shank, with a round cap on top (under the eye)
    draw.rectangle([*xy(SHANK_TOP[0] - HALF, SHANK_TOP[1]), *xy(SHANK_BOTTOM[0] + HALF, SHANK_BOTTOM[1])], fill=BROWN)
    disc(SHANK_TOP, HALF)
    # the bend: a 270-degree band between radius 72-15 and 72+15; it meets the shank's end square on
    steps = 1080
    angles = [math.radians(270 * i / steps) for i in range(steps + 1)]
    outer = [xy(BEND[0] + (BEND_R + HALF) * math.cos(a), BEND[1] + (BEND_R + HALF) * math.sin(a)) for a in angles]
    inner = [xy(BEND[0] + (BEND_R - HALF) * math.cos(a), BEND[1] + (BEND_R - HALF) * math.sin(a)) for a in reversed(angles)]
    draw.polygon(outer + inner, fill=BROWN)
    disc(POINT, HALF)
    disc(EYE, EYE_R)
    return img.reduce(ss)


def hook_reach() -> float:
    """How far the hook reaches from the centre of the 512 box (for the maskable safe zone)."""
    c = BOX / 2
    return max(math.dist(EYE, (c, c)) + EYE_R, math.dist(BEND, (c, c)) + BEND_R + HALF,
               math.dist(SHANK_BOTTOM, (c, c)) + HALF)


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    written = []

    def save(img: Image.Image, path: Path, **kw) -> None:
        img.save(path, **kw)
        written.append(path)

    save(render(512), ICONS / "baitbook.png", optimize=True)

    ico = [render(n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    save(ico[-1], ICONS / "baitbook.ico", sizes=[im.size for im in ico], append_images=ico[:-1])

    # ICNS: Pillow writes it on any platform (PNG entries ic07-ic14); the 1024 base is ic10
    icns = [render(n, fill=MAC_GRID) for n in (32, 64, 128, 256, 512)]
    save(render(1024, fill=MAC_GRID), ICONS / "baitbook.icns", append_images=icns)

    save(render(192), DEVICE / "icon-192.png", optimize=True)
    save(render(512), DEVICE / "icon-512.png", optimize=True)
    # maskable: the safe zone is the centred circle of radius 40% (204.8 of 512); the hook as drawn fits
    assert hook_reach() <= 0.4 * BOX, "the hook no longer fits the maskable safe zone: give it a smaller fill"
    save(render(512, full_bleed=True).convert("RGB"), DEVICE / "icon-maskable-512.png", optimize=True)
    save(render(180, full_bleed=True).convert("RGB"), DEVICE / "apple-touch-icon.png", optimize=True)

    for p in written:
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size // 1024 or 1} KB")


if __name__ == "__main__":
    main()
