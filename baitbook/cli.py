"""The `baitbook` command.

  baitbook             open the PC app (starts the Angler in the background if it isn't running)
  baitbook angler      run the Angler here, in this console; Ctrl+C stops it
  baitbook stop        stop the Angler running in the background
  baitbook status      is it running, where the config and data live, the pairing code
  baitbook phone       put the Angler behind Tailscale Serve, so the phone app works offline
  baitbook setup      check the models and ComfyUI, fill in what's missing, add the app icon
  baitbook comfyui     install (or repair) baitbook's own ComfyUI, with PyTorch for this GPU
  baitbook models      download the default models from Hugging Face (picks up where it stopped)
  baitbook shortcut    add the app icon to the Start menu / Applications / app menu

`baitbook-app` is the PC app with no console window: what the app icon opens.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__


def local_url() -> str:
    """The Angler on this machine, as the PC app reaches it: plain HTTP on loopback, on the port
    one that's running noted (`baitbook angler --port`), else the config's."""
    from angler import config

    return f"http://127.0.0.1:{config.angler_port(config.load()['server'])}"


def call(path: str, method: str = "GET", timeout: float = 3) -> dict | None:
    """The local Angler's answer as JSON, or None if nothing answers."""
    req = urllib.request.Request(local_url() + path, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def busy(status: dict) -> bool:
    return status["engine"]["state"] not in ("idle", "error")


# ---------- commands ----------
def cmd_app(_args) -> int:
    from pcapp.__main__ import main as app_main

    app_main()
    return 0


def cmd_angler(args) -> int:
    from angler.__main__ import main as angler_main

    return angler_main([*(["--host", args.host] if args.host else []), *(["--port", str(args.port)] if args.port else [])]) or 0


def cmd_stop(args) -> int:
    status = call("/api/status")
    if status is None:
        print("No Angler is running on this machine.")
        return 0
    if busy(status) and not args.force:
        e = status["engine"]
        print(f"The Angler is busy ({e.get('phase') or e['state']}). `baitbook stop --force` stops it anyway; "
              "a night shift cut off now is marked interrupted.")
        return 1
    call("/api/shutdown", "POST")
    for _ in range(60):  # it answers first, then stops
        if call("/api/status", timeout=1) is None:
            print("The Angler has stopped.")
            return 0
        time.sleep(0.5)
    print("The Angler didn't stop within 30 s.", file=sys.stderr)
    return 1


def cmd_status(_args) -> int:
    from angler import config

    config.load()
    print(f"Config  {config.CONFIG_PATH}\nData    {config.DATA}")
    status = call("/api/status")
    if status is None:
        print("Angler  not running (`baitbook` or `baitbook angler` starts it)")
        return 1
    e, c = status["engine"], status["counts"]
    print(f"Angler  {local_url()}  ·  {e.get('phase') or e['state']}")
    if status.get("code") != config.code_version():
        print("        running older code than this: `baitbook stop`, then `baitbook` (or restart it in its console)")
    print(f"        {c['residents']} residents · {c['posts']} edits · {c['bites']} bites · {c['snags']} snags")
    if status.get("pair_code"):
        print(f"Pairing code  {status['pair_code']}")
    if status.get("tailscale_url"):
        print(f"Phone   {status['tailscale_url']}  (through Tailscale: works offline)")
    lan = status.get("lan_urls") or []
    for url in lan:
        print(f"Phone   {url}" + ("  (same Wi-Fi only; offline needs HTTPS)" if url.startswith("http:") else ""))
    if not status.get("tailscale_url") and not (lan and all(u.startswith("https:") for u in lan)):
        print("        For offline mode on the phone: `baitbook phone`")
    return 0


PHONE_STEPS = """\
On the phone:
  1. Install Tailscale ({download}) and sign in with the same account as this PC.
  2. With Tailscale connected, scan the code or open the address. The first visit can take
     half a minute while Tailscale gets the certificate.
  3. Add it to the home screen (iPhone: Share > Add to Home Screen; Android: menu > Add to
     Home screen), open it from there and pair with {code}.
After that the app opens with no network at all; docking needs Tailscale connected."""


def cmd_phone(_args) -> int:
    """Put the Angler behind `tailscale serve`, so the phone app gets HTTPS and works offline,
    and show the phone where to go."""
    import segno
    from angler import config, tailscale

    port = config.local_port(config.load()["server"])
    target = f"http://127.0.0.1:{port}"
    again = "then run `baitbook phone` again."
    ts = tailscale.exe()
    if not ts:
        print(f"Tailscale isn't installed on this PC. Get it from {tailscale.DOWNLOAD}, sign in, {again}")
        return 1
    me = tailscale.machine()
    if me is None or not me.running:
        print(f"Tailscale isn't connected on this PC: open it and sign in, {again}")
        return 1
    if not me.name:
        print(f"Turn on MagicDNS and HTTPS Certificates for your tailnet at {tailscale.ADMIN_DNS}, {again}")
        return 1
    serving = tailscale.serve_config() or {}
    url = tailscale.serve_url(port, serving)
    if url:
        print("Tailscale Serve already passes this Angler on.")
    else:
        taken = tailscale.root_taken(me.name, port, serving)
        if taken:
            print(f"`tailscale serve` already passes https://{me.name}/ on to {taken}, and serving the Angler there would "
                  f"replace it. To put the Angler on another port instead, run\n  tailscale serve --bg --https=8443 {target}\n"
                  "and baitbook will find it there.")
            return 1
        if not me.https:
            print("HTTPS isn't on for your tailnet yet. If Tailscale shows a link to turn it on, open it and approve; this carries on.")
        print(f"Running: tailscale serve --bg {target}\n", flush=True)
        if subprocess.run([ts, "serve", "--bg", target]).returncode != 0:
            print(f"\n`tailscale serve` didn't finish. Check that MagicDNS and HTTPS Certificates are on at {tailscale.ADMIN_DNS}, {again}",
                  file=sys.stderr)
            return 1
        url = tailscale.serve_url(port) or f"https://{me.name}/device/"
    status = call("/api/status")
    code = f"the code {status['pair_code']}" if status and status.get("pair_code") else \
        "the code the PC app shows in Settings (start it with `baitbook`)"
    print(f"\nThe phone app is at\n\n  {url}\n")
    segno.make(url, error="m").terminal(compact=True)
    print("\n" + PHONE_STEPS.format(download=tailscale.DOWNLOAD, code=code))
    return 0


def cmd_setup(args) -> int:
    from .setup import run

    return run(check=args.check)


def cmd_comfyui(args) -> int:
    from . import comfyui
    from .gpu import Gpu, detect, detect_hardware

    if call("/api/status") is not None:
        print("The Angler is running; `baitbook stop` it first, so the ComfyUI it may have started isn't in use.")
        return 1
    gpu = detect()
    if args.gpu and args.gpu != gpu.kind:  # keep what was found (an AMD GPU's architecture, say) when it agrees
        found = detect_hardware()
        gpu = found if found.kind == args.gpu else Gpu(args.gpu, "chosen with --gpu")
    try:
        comfyui.install(gpu, torch_again=args.torch)
    except comfyui.InstallError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_models(args) -> int:
    from . import models

    return models.run(yes=args.yes)


def cmd_shortcut(args) -> int:
    from . import shortcut

    try:
        done = shortcut.remove() if args.remove else shortcut.create(desktop=args.desktop)
    except (RuntimeError, OSError) as exc:
        print(f"Couldn't {'remove' if args.remove else 'add'} the app icon: {exc}", file=sys.stderr)
        return 1
    for p in done:
        print(("removed " if args.remove else "added ") + str(p))
    if not done:
        print("nothing to remove" if args.remove else "no icon was added")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="baitbook", description="A social feed with no humans in it.")
    ap.add_argument("--version", action="version", version=f"baitbook {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.add_parser("app", help="open the PC app (the default)").set_defaults(fn=cmd_app)
    a = sub.add_parser("angler", help="run the Angler here, in this console")
    a.add_argument("--host", help="override [server] host")
    a.add_argument("--port", type=int, help="override [server] port")
    a.set_defaults(fn=cmd_angler)
    s = sub.add_parser("stop", help="stop the Angler running in the background")
    s.add_argument("--force", action="store_true", help="stop it even in the middle of a job")
    s.set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="is it running, and where things live").set_defaults(fn=cmd_status)
    sub.add_parser("phone", help="put the Angler behind Tailscale Serve, so the phone app works offline").set_defaults(fn=cmd_phone)
    st = sub.add_parser("setup", help="check the models and ComfyUI, fill in what's missing, add the app icon")
    st.add_argument("--check", action="store_true", help="only check: ask nothing, change nothing (exit 1 if something's missing)")
    st.set_defaults(fn=cmd_setup)
    cu = sub.add_parser("comfyui", help="install (or repair) baitbook's own ComfyUI, with PyTorch for this GPU")
    cu.add_argument("--gpu", choices=("nvidia", "amd", "intel", "mps", "cpu"), help="the PyTorch build to install (default: detected)")
    cu.add_argument("--torch", action="store_true", help="install PyTorch again (after a driver update, or with another --gpu)")
    cu.set_defaults(fn=cmd_comfyui)
    md = sub.add_parser("models", help="download the default models from Hugging Face (picks up where it stopped)")
    md.add_argument("--yes", "-y", action="store_true", help="download without asking first")
    md.set_defaults(fn=cmd_models)
    sc = sub.add_parser("shortcut", help="add the app icon")
    sc.add_argument("--desktop", action="store_true", help="put one on the desktop too")
    sc.add_argument("--remove", action="store_true", help="take the app icon away")
    sc.set_defaults(fn=cmd_shortcut)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # paths and the odd "·" survive a redirect on Windows
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(getattr(args, "fn", cmd_app)(args))


def app() -> None:
    """`baitbook-app`: the PC app, with no console window."""
    sys.exit(cmd_app(None))
