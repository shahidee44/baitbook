"""angler/media.py: the PNG master and its JPEG variants, served safely."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from angler import config, media
from tests.support import png


@pytest.fixture(autouse=True)
def loaded(home):
    config.load()


def test_save_makes_png_jpg_and_thumbnail():
    stem = media.save(png((255, 0, 0), (2000, 1000)))
    files = {p.name for p in config.IMAGES.iterdir()}
    assert files == {f"{stem}.png", f"{stem}.jpg", f"{stem}_t.jpg"}
    with Image.open(config.IMAGES / f"{stem}.png") as im:
        assert (im.format, im.size, im.mode) == ("PNG", (2000, 1000), "RGB")
    with Image.open(config.IMAGES / f"{stem}.jpg") as im:
        assert im.format == "JPEG" and im.size == (media.FULL_JPEG, media.FULL_JPEG // 2)
    with Image.open(config.IMAGES / f"{stem}_t.jpg") as im:
        assert im.format == "JPEG" and im.size == (media.THUMB, media.THUMB)


def test_save_keeps_small_images_small_and_flattens_alpha():
    stem = media.save(png((0, 0, 255), (40, 30), mode="RGBA"))
    with Image.open(config.IMAGES / f"{stem}.png") as im:
        assert im.mode == "RGB" and im.size == (40, 30)
    with Image.open(config.IMAGES / f"{stem}.jpg") as im:
        assert im.size == (40, 30)


def test_save_accepts_a_jpeg_and_gives_unique_stems():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (9, 9, 9)).save(buf, "JPEG")
    a, b = media.save(buf.getvalue()), media.save(buf.getvalue())
    assert a != b and len(a) == 16
    assert media.png(a)[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("data", [b"not an image", png()[:40]])  # garbage, and a PNG cut off after its header
def test_save_rejects_garbage_and_writes_nothing(data):
    with pytest.raises(media.NotAnImage) as caught:
        media.save(data)
    assert isinstance(caught.value, ValueError)
    assert list(config.IMAGES.iterdir()) == []


def test_save_that_fails_halfway_leaves_no_files(monkeypatch):
    def disk_full(stem, im):
        raise OSError("no space left on device")

    monkeypatch.setattr(media, "_variants", disk_full)
    with pytest.raises(OSError, match="no space"):
        media.save(png())
    assert list(config.IMAGES.iterdir()) == []  # the PNG master written first went too


def test_save_applies_exif_orientation():
    im = Image.new("RGB", (40, 20), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise on display
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    stem = media.save(buf.getvalue())
    with Image.open(config.IMAGES / f"{stem}.png") as out:
        assert out.size == (20, 40)


def test_path_serves_only_files_in_the_images_folder():
    stem = media.save(png())
    for name in (f"{stem}.jpg", f"{stem}_t.jpg", f"{stem}.png"):
        assert media.path(name) == (config.IMAGES / name).resolve()
    assert media.path("nope.jpg") is None
    assert media.path("") is None


@pytest.mark.parametrize("name", ["../baitbook.toml", "..\\baitbook.toml", "../../home/baitbook.toml", "./../baitbook.toml",
                                  "../data/baitbook.db", "..", "."])
def test_path_refuses_traversal(name):
    config.load()
    assert config.CONFIG_PATH.is_file()
    assert media.path(name) is None


def test_path_refuses_an_absolute_path():
    assert media.path(str(config.CONFIG_PATH.resolve())) is None


def test_path_refuses_a_subfolder():
    sub = config.IMAGES / "sub"
    sub.mkdir()
    (sub / "x.jpg").write_bytes(b"x")
    assert media.path("sub/x.jpg") is None


def test_delete_removes_every_variant():
    stem = media.save(png())
    media.delete(stem)
    assert list(config.IMAGES.iterdir()) == []
    media.delete(stem)  # again: nothing to do, no error


def test_avg_color():
    stem = media.save(png((255, 0, 0), (64, 64)))
    r, g, b = (int(media.avg_color(stem)[i:i + 2], 16) for i in (1, 3, 5))
    assert r > 230 and g < 25 and b < 25  # red, give or take the JPEG
    assert media.avg_color("missing") == "#c9a27a"
