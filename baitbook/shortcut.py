"""The app icon: put baitbook where apps are opened from (`baitbook shortcut`, `baitbook setup`).

  Windows  baitbook.lnk in the Start menu (and on the desktop if asked)
  macOS    ~/Applications/baitbook.app, a small app bundle (and a link to it on the desktop)
  Linux    ~/.local/share/applications/baitbook.desktop (and a copy on the desktop)

Each one opens `baitbook-app`, the PC app with no console window. No admin rights needed.
BAITBOOK_SHORTCUT_DIR puts them all in that one folder instead (the menu entry in it, the
desktop one in its Desktop subfolder), so tests never touch the real menus.
"""

from __future__ import annotations

import base64
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

NAME = "baitbook"
ICONS = Path(__file__).resolve().parent / "icons"
COMMENT = "A social feed with no humans in it"
BUNDLE_ID = "io.github.shahidee44.baitbook"


def create(desktop: bool = False) -> list[Path]:
    """Add the app icon (and one on the desktop). Returns the paths written.
    Raises RuntimeError (Windows: PowerShell failed) or OSError, with a message fit to show."""
    if sys.platform == "win32":
        return _windows_create(desktop)
    if sys.platform == "darwin":
        return _mac_create(desktop)
    return _linux_create(desktop)


def remove() -> list[Path]:
    """Take the app icon away, from the menu and the desktop. Returns the paths removed."""
    if sys.platform == "win32":
        folders, name = _windows_folders(), f"{NAME}.lnk"
    elif sys.platform == "darwin":
        folders, name = _mac_folders(), f"{NAME}.app"
    else:
        folders, name = _linux_folders(), f"{NAME}.desktop"
    gone = []
    for p in (folder / name for folder in folders):
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.is_symlink() or p.exists():
            p.unlink()
        else:
            continue
        gone.append(p)
    return gone


def launcher() -> tuple[list[str], Path | None]:
    """What the icon runs, and the folder to run it in (None: anywhere).

    The `baitbook-app` launcher next to this Python (a venv's Scripts/ or bin/, as uv tool
    installs it) or on PATH; failing that (a source checkout), `python -m baitbook app` run
    from the folder that holds the baitbook package."""
    exe = "baitbook-app.exe" if sys.platform == "win32" else "baitbook-app"
    beside = Path(sys.executable).parent / exe
    if beside.is_file():
        return [str(beside)], None
    if found := shutil.which("baitbook-app"):
        return [str(Path(found).absolute())], None
    python = Path(sys.executable)
    if sys.platform == "win32" and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")  # no console window
    return [str(python), "-m", "baitbook", "app"], Path(__file__).resolve().parents[1]


def _override() -> Path | None:
    d = os.environ.get("BAITBOOK_SHORTCUT_DIR")
    return Path(d).expanduser().absolute() if d else None


# ---------- Windows: a .lnk made through PowerShell's WScript.Shell ----------
def _powershell(script: str, env: dict[str, str] | None = None) -> str:
    """Run a PowerShell script (passed encoded, so no quoting can break it); its output, as text."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = ("$ErrorActionPreference = 'Stop'\n"
              "try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch {}\n" + script)
    r = subprocess.run([str(exe) if exe.is_file() else "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
                       capture_output=True, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode:
        err = r.stderr.decode(errors="replace").strip()
        if err.startswith("#< CLIXML"):  # PowerShell's serialized error stream: pull the message out
            err = " ".join(re.findall(r'<S S="Error">(.*?)</S>', err)).replace("_x000D__x000A_", " ").strip() or err
        raise RuntimeError(f"couldn't place the app icon; PowerShell said: {err or out.strip() or f'exit status {r.returncode}'}")
    return out


def _windows_folders() -> tuple[Path, Path]:
    """The Start menu's Programs folder and the desktop (wherever it is: OneDrive moves it)."""
    if d := _override():
        return d, d / "Desktop"
    lines = _powershell("[Environment]::GetFolderPath('Programs')\n[Environment]::GetFolderPath('Desktop')").splitlines()
    programs, desktop = (lines + ["", ""])[:2]
    if not programs.strip():
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        programs = str(appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return Path(programs.strip()), Path(desktop.strip() or Path.home() / "Desktop")


def _windows_create(desktop: bool) -> list[Path]:
    menu, desk = _windows_folders()
    links = [menu / f"{NAME}.lnk"] + ([desk / f"{NAME}.lnk"] if desktop else [])
    for p in links:
        p.parent.mkdir(parents=True, exist_ok=True)
    cmd, cwd = launcher()
    _powershell(
        "$shell = New-Object -ComObject WScript.Shell\n"
        "foreach ($path in $env:BB_LINKS -split \"`n\") {\n"
        "  $s = $shell.CreateShortcut($path)\n"
        "  $s.TargetPath = $env:BB_TARGET\n"
        "  $s.Arguments = [string]$env:BB_ARGS\n"
        "  $s.WorkingDirectory = $env:BB_CWD\n"
        "  $s.IconLocation = $env:BB_ICON + ',0'\n"
        "  $s.Description = $env:BB_COMMENT\n"
        "  $s.Save()\n"
        "}",
        env={"BB_LINKS": "\n".join(map(str, links)), "BB_TARGET": cmd[0], "BB_ARGS": subprocess.list2cmdline(cmd[1:]),
             "BB_CWD": str(cwd or Path.home()), "BB_ICON": str(ICONS / f"{NAME}.ico"), "BB_COMMENT": COMMENT})
    return links


# ---------- macOS: a minimal .app bundle ----------
def _mac_folders() -> tuple[Path, Path]:
    if d := _override():
        return d, d / "Desktop"
    return Path.home() / "Applications", Path.home() / "Desktop"


def _mac_create(desktop: bool) -> list[Path]:
    apps, desk = _mac_folders()
    app = apps / f"{NAME}.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": NAME,
        "CFBundleIconFile": f"{NAME}.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }))
    cmd, cwd = launcher()
    run = contents / "MacOS" / NAME
    run.write_text("#!/bin/sh\n" + (f"cd {shlex.quote(str(cwd))} || exit 1\n" if cwd else "")
                   + f'exec {shlex.join(cmd)} "$@"\n', encoding="utf-8")
    run.chmod(0o755)
    shutil.copyfile(ICONS / f"{NAME}.icns", contents / "Resources" / f"{NAME}.icns")
    os.utime(app)  # a newer date tells Finder to look at the icon again
    made = [app]
    if desktop:
        link = desk / f"{NAME}.app"
        desk.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        if not link.exists():  # a real folder there isn't ours to replace
            link.symlink_to(app, target_is_directory=True)
            made.append(link)
    return made


# ---------- Linux (and other freedesktop systems): a .desktop entry ----------
def _linux_folders() -> tuple[Path, Path]:
    if d := _override():
        return d, d / "Desktop"
    data = os.environ.get("XDG_DATA_HOME", "")
    apps = (Path(data) if os.path.isabs(data) else Path.home() / ".local" / "share") / "applications"
    desk = ""
    if shutil.which("xdg-user-dir"):  # the desktop's real name, which may be translated
        try:
            desk = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return apps, Path(desk) if desk and desk != str(Path.home()) else Path.home() / "Desktop"


def _desktop_escape(value: str) -> str:
    """A string value in a .desktop file: backslashes doubled (and no newlines)."""
    return value.replace("\\", "\\\\").replace("\n", " ")


def _exec_arg(arg: str) -> str:
    """One argument of Exec=, quoted as the Desktop Entry spec wants (then escaped as a string)."""
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg.replace("%", "%%")) + '"'


def _linux_create(desktop: bool) -> list[Path]:
    menu, desk = _linux_folders()
    cmd, cwd = launcher()
    lines = ["[Desktop Entry]", "Type=Application", "Version=1.0", f"Name={NAME}", f"Comment={COMMENT}",
             "Exec=" + _desktop_escape(" ".join(_exec_arg(a) for a in cmd)),
             "Icon=" + _desktop_escape(str(ICONS / f"{NAME}.png")),
             "Terminal=false", "Categories=Network;Graphics;", "StartupNotify=true"]
    if cwd:
        lines.append("Path=" + _desktop_escape(str(cwd)))
    text = "\n".join(lines) + "\n"
    entry = menu / f"{NAME}.desktop"
    menu.mkdir(parents=True, exist_ok=True)
    entry.write_text(text, encoding="utf-8")
    made = [entry]
    if desktop:
        copy = desk / f"{NAME}.desktop"
        desk.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
        copy.chmod(0o755)  # a launcher on the desktop has to be executable...
        if not _override() and shutil.which("gio"):  # ...and, on GNOME, marked trusted
            try:
                subprocess.run(["gio", "set", str(copy), "metadata::trusted", "true"], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass  # it still works; GNOME asks once before running it
        made.append(copy)
    return made
