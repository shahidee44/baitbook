"""`baitbook models`: download the default models from Hugging Face and point the config
at them. `baitbook setup` offers it for whatever's missing.

  [text]  model, mmproj             Gemma 4 31B (QAT GGUF)         into <home>/models
  [image] unet, text_encoder, vae   FLUX.2 [klein] 4B and its kin  into ComfyUI's models/<folder>

Each file is pinned to a commit, with its size and sha256, so what lands is what was
tested. It downloads to <name>.part, which the next run resumes (after Ctrl+C or a lost
connection), and is renamed into place once its size and sha256 check out. A setting
that names some other file (klein 9B, say) is the user's choice and is left alone.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

HF = "https://huggingface.co"


@dataclass(frozen=True)
class Item:
    """One default file: the setting it fills, and exactly what to download for it."""

    section: str  # "text" or "image"
    key: str
    repo: str
    revision: str  # a commit, so the file can't change under us
    file: str  # its path in the repo
    size: int
    sha256: str
    folder: str = ""  # [image] files: the folder inside ComfyUI's models

    @property
    def name(self) -> str:
        return self.file.rsplit("/", 1)[-1]

    @property
    def url(self) -> str:
        return f"{HF}/{self.repo}/resolve/{self.revision}/{self.file}"

    @property
    def setting(self) -> str:
        return f"[{self.section}] {self.key}"


# All Apache 2.0 and ungated. Sizes and sha256s are the Hub's (the LFS pointers).
GEMMA = "google/gemma-4-31B-it-qat-q4_0-gguf", "59dde24573e7e61570dba08b18a2e1fe246955ed"
KLEIN = "black-forest-labs/FLUX.2-klein-4B", "e7b7dc27f91deacad38e78976d1f2b499d76a294"
KLEIN_EXTRAS = "Comfy-Org/vae-text-encorder-for-flux-klein-4b", "5f526678002e43af5551dadb73ce2e8c91b43afe"
DEFAULTS = (
    Item("text", "model", *GEMMA, "gemma-4-31B_q4_0-it.gguf", 17_651_001_568,
         "179cfb99212709597eae5929112cfca677e1bbf566178b479ae1da0c4772874b"),
    Item("text", "mmproj", *GEMMA, "gemma-4-31B-it-mmproj.gguf", 1_200_726_368,
         "6bd60bdb958548b4093196d38744b0f2290c12503a3fddd7486bffa9c5eb07a4"),
    Item("image", "unet", *KLEIN, "flux-2-klein-4b.safetensors", 7_751_105_712,
         "ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343", "diffusion_models"),
    Item("image", "text_encoder", *KLEIN_EXTRAS, "split_files/text_encoders/qwen_3_4b.safetensors", 8_044_982_048,
         "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a", "text_encoders"),
    Item("image", "vae", *KLEIN_EXTRAS, "split_files/vae/flux2-vae.safetensors", 336_211_292,
         "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3", "vae"),
)
OLD_FOLDERS = {"diffusion_models": ("unet",), "text_encoders": ("clip",)}  # ComfyUI still looks in these too

MARGIN = 1 << 30  # disk left free after the downloads
CHUNK = 1 << 20
TIMEOUT = 60  # seconds without a byte before a connection counts as lost
RETRIES = 5  # in a row with nothing downloaded
BACKOFF = 2.0  # seconds before the first retry, doubled each time (up to a minute)
NETWORK = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ssl.SSLError)


class DownloadError(RuntimeError):
    pass


def say(ok: bool | None, text: str) -> None:
    print(f"  {'ok' if ok else '--' if ok is None else '!!'}  {text}")


def confirm(prompt: str) -> bool:
    try:
        return input(f"      {prompt} [Y/n]: ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):  # no terminal to answer from, or Ctrl+C: no
        print()
        return False


def gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------- what's there ----------
def look(item: Item) -> tuple[str, Path | None]:
    """Where a default stands: ("ok", the file); ("own", the setting) when it names some
    other file, which is left alone; or ("missing", the folder it would go into, None for
    an [image] file with no ComfyUI set up)."""
    from angler import config

    from .comfyui import models_dir

    value = str(config.section(item.section).get(item.key) or "")
    if item.section == "text":
        if value and Path(value).name != item.name:
            return "own", Path(value)
        p = config.path(value)
        return ("ok", p) if p and p.is_file() else ("missing", config.HOME / "models")
    if value and value != item.name:
        return "own", Path(value)
    models = models_dir(config.section("image"))
    if models is None:
        return "missing", None
    for folder in (item.folder, *OLD_FOLDERS.get(item.folder, ())):
        if found := next((p for p in (models / folder).rglob(item.name) if p.is_file()), None):
            return "ok", found
    return "missing", models / item.folder


def wanted() -> list[tuple[Item, Path | None]]:
    """The defaults that aren't there yet, each with the folder it goes into (None: no ComfyUI)."""
    found = []
    for item in DEFAULTS:
        state, where = look(item)
        if state == "missing":
            found.append((item, where))
    return found


def missing() -> list[Item]:
    """The defaults that aren't there yet (a setting that names some other file doesn't count)."""
    return [item for item, _ in wanted()]


def left(item: Item, folder: Path) -> int:
    """Bytes still to download: none if the file is there, less whatever its .part has."""
    if (folder / item.name).is_file():
        return 0
    part = folder / f"{item.name}.part"
    have = part.stat().st_size if part.is_file() else 0
    return item.size - have if have <= item.size else item.size


def show(wanted: list[tuple[Item, Path | None]]) -> list[tuple[Item, Path]]:
    """Say what's to download (and what can't be yet); returns the (item, folder) pairs to download."""
    todo = [(item, folder) for item, folder in wanted if folder]
    if waiting := [item.name for item, folder in wanted if not folder]:
        say(None, f"{', '.join(waiting)}: set up ComfyUI first (`baitbook comfyui`); they go into its models folder")
    if todo:
        say(False, f"default models to download from Hugging Face, {gb(sum(left(i, f) for i, f in todo))} in all:")
        width = max(len(item.name) for item, _ in todo)
        for item, folder in todo:
            done = item.size - left(item, folder)
            print(f"        {item.name:<{width}}  {gb(item.size):>9}  into {folder}"
                  + (f"  ({gb(done)} already here)" if done else ""))
    return todo


def no_room(todo: list[tuple[Item, Path]]) -> str | None:
    """None if each disk has room for what's left to download (and MARGIN to spare), or what's short."""
    disks: dict[int, list] = {}
    for item, folder in todo:
        where = folder
        while not where.exists() and where != where.parent:
            where = where.parent
        disks.setdefault(os.stat(where).st_dev, [where, 0])[1] += left(item, folder)
    for where, need in disks.values():
        free = shutil.disk_usage(where).free
        if need and free < need + MARGIN:
            return (f"not enough disk space in {where}: {gb(need)} to download, and {gb(MARGIN)} to spare, "
                    f"but {gb(free)} free. Make room and run this again.")
    return None


# ---------- downloading ----------
class Progress:
    """How far a file has got: one line updated in place on a terminal, a plain line
    every 10% otherwise. Name, GB done / total, MB/s, time left."""

    def __init__(self, label: str, total: int, done: int = 0) -> None:
        self.label, self.total, self.done = label, total, done
        self.tty = sys.stdout is not None and sys.stdout.isatty()
        self.restart(done)

    def restart(self, done: int) -> None:
        """Measure the speed from here (a new connection)."""
        self.done = self.first = done
        self.t0, self.drawn, self.tenth, self.width = time.monotonic(), float("-inf"), -1, 0

    def text(self) -> str:
        line = f"  > {self.label}  {self.done / 1e9:.2f} / {gb(self.total)}"
        elapsed = time.monotonic() - self.t0
        if elapsed >= 1 and self.done > self.first:
            rate = (self.done - self.first) / elapsed
            line += f"  {rate / 1e6:.1f} MB/s  ETA {clock((self.total - self.done) / rate)}"
        return line

    def update(self, done: int) -> None:
        self.done = done
        if self.tty:
            now = time.monotonic()
            if now - self.drawn >= 0.25:
                self.draw()
                self.drawn = now
        elif (tenth := done * 10 // max(self.total, 1)) > self.tenth:
            self.tenth = tenth
            print(self.text(), flush=True)

    def draw(self) -> None:
        line = self.text()
        sys.stdout.write("\r" + line.ljust(self.width))
        sys.stdout.flush()
        self.width = len(line)

    def end(self) -> None:
        """Finish the line on a terminal, showing where it got to."""
        if self.tty and self.width:
            self.draw()
            print(flush=True)
            self.width = 0

    def note(self, text: str, done: int) -> None:
        self.end()
        print(f"      {text}", flush=True)
        self.restart(done)

    def __enter__(self) -> Progress:
        self.update(self.done)
        return self

    def __exit__(self, *exc) -> None:
        self.end()


def request(item: Item, start: int) -> urllib.request.Request:
    headers = {"User-Agent": f"baitbook/{__version__}"}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.Request(item.url, headers=headers)


def fetch(item: Item, folder: Path) -> Path:
    """Download one file into folder: resume its .part, check the size and sha256, then
    rename it into place. DownloadError if it can't; Ctrl+C keeps the .part."""
    folder.mkdir(parents=True, exist_ok=True)
    final, part = folder / item.name, folder / f"{item.name}.part"
    if final.is_file():
        if final.stat().st_size == item.size:
            return final  # downloaded before; only the setting was missing
        raise DownloadError(f"{final} is there already but isn't the file expected ({gb(item.size)}). "
                            "Move it away and run this again.")
    sha, have = hashlib.sha256(), 0
    if part.is_file() and part.stat().st_size > item.size:
        part.unlink()  # longer than the file: not ours
    if part.is_file():  # resuming: what's there is read again, so the sha256 covers the whole file
        with Progress(f"checking {item.name}", part.stat().st_size) as progress, open(part, "rb") as f:
            while chunk := f.read(CHUNK):
                sha.update(chunk)
                have += len(chunk)
                progress.update(have)

    tries = 0
    with Progress(item.name, item.size, have) as progress:
        while have < item.size:
            start = have
            try:
                with urllib.request.urlopen(request(item, have), timeout=TIMEOUT) as r:
                    if have and (r.status != 206 or not r.headers.get("Content-Range", "").startswith(f"bytes {have}-")):
                        # the server ignored the Range (a 200: here's the whole file) or sent some other part
                        progress.note(f"{item.name}: the server can't resume it; starting again", 0)
                        have, sha = 0, hashlib.sha256()
                        if r.status != 200:
                            part.unlink()
                            continue  # and ask for the whole file
                    length = r.headers.get("Content-Length")
                    if length and have + int(length) != item.size:
                        raise DownloadError(f"{item.name}: Hugging Face is sending {int(length):,} bytes from byte {have:,}, "
                                            f"but the file is {item.size:,}. Is {item.url} still the file?")
                    with open(part, "ab" if have else "wb") as f:
                        while chunk := r.read(CHUNK):
                            f.write(chunk)
                            sha.update(chunk)
                            have += len(chunk)
                            progress.update(have)
                        f.flush()
                        os.fsync(f.fileno())
                if have < item.size:
                    raise ConnectionError("the connection closed early")
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code not in (408, 429):
                    if exc.code == 416:  # the .part doesn't fit the file: start afresh next time
                        part.unlink(missing_ok=True)
                    raise DownloadError(f"{item.name}: {exc.code} {exc.reason} from {item.url}") from None
                err = exc
            except NETWORK as exc:
                err = exc
            else:
                continue
            tries = 1 if have > start else tries + 1
            if tries > RETRIES:
                raise DownloadError(f"{item.name}: the download keeps failing ({err}). What's downloaded so far is "
                                    "kept; run this again to pick up from there.")
            wait = min(BACKOFF * 2 ** (tries - 1), 60)
            progress.note(f"{item.name}: {err}; trying again in {wait:.0f} s", have)
            time.sleep(wait)

    if have != item.size or sha.hexdigest() != item.sha256:
        part.unlink(missing_ok=True)
        what = "size" if have != item.size else "sha256"
        raise DownloadError(f"{item.name} didn't download correctly (its {what} doesn't match Hugging Face's), "
                            "so it was deleted. Run this again to download it afresh.")
    os.replace(part, final)
    return final


def use(item: Item, path: Path) -> None:
    """Point the setting at the file: its full path for [text], for [image] the name ComfyUI knows it by."""
    from angler import config

    if item.section == "text":
        config.update({"text": {item.key: str(path.absolute())}})
    elif config.section("image").get(item.key) != item.name:
        config.update({"image": {item.key: item.name}})


def download(todo: list[tuple[Item, Path]]) -> int:
    """Download each file and point its setting at it; the number that failed. Ctrl+C
    stops (KeyboardInterrupt) and keeps the .part."""
    if short := no_room(todo):
        say(False, short)
        return len(todo)
    failed = 0
    for item, folder in todo:
        try:
            path = fetch(item, folder)
        except KeyboardInterrupt:
            part = folder / f"{item.name}.part"
            say(None, f"stopped. {part} keeps what's downloaded so far; `baitbook models` picks up from there."
                if part.is_file() else "stopped.")
            raise
        except DownloadError as exc:
            say(False, str(exc))
            failed += 1
            continue
        except OSError as exc:  # writing it: a full disk, no permission
            say(False, f"{item.name}: couldn't write it into {folder} ({exc})")
            failed += 1
            continue
        use(item, path)
        say(True, f"{item.setting}: {path}")
    return failed


# ---------- setup and `baitbook models` ----------
def offer(ask: bool) -> int:
    """For `baitbook setup`: say which defaults are missing, ask before downloading them
    (when ask), download them, and return how many are still missing."""
    todo = show(wanted())
    if todo and (not ask or confirm("download them now?")):
        try:
            download(todo)
        except KeyboardInterrupt:
            pass
    return len(missing())


def run(yes: bool = False) -> int:
    """`baitbook models`: download whichever defaults are missing, asking first unless yes.
    0 once they're all in place."""
    from angler import config

    config.load()
    print(f"baitbook models\n  config  {config.CONFIG_PATH}\n")
    need = []
    for item in DEFAULTS:
        state, where = look(item)
        if state == "ok":
            say(True, f"{item.setting}: {where}")
        elif state == "own":
            say(None, f"{item.setting}: {where} (your own choice, left alone)")
        else:
            need.append((item, where))
    if not need:
        print("\nAll the default models are in place.")
        return 0
    todo = show(need)
    if todo and not (yes or confirm("download them now?")):
        print("\nNothing downloaded. `baitbook models --yes` downloads them without asking.")
        return 1
    try:
        download(todo)
    except KeyboardInterrupt:
        return 130
    still = len(missing())
    if still:
        print(f"\n{still} still missing; run `baitbook models` again once the problem above is sorted.")
        return 1
    print("\nAll the default models are in place.")
    return 0
