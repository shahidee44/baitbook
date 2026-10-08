"""Tailscale, for the phone over HTTPS: what this machine's Tailscale is doing, and the address
`tailscale serve` gives this Angler.

The phone app only works offline over HTTPS (a service worker needs a secure context).
`tailscale serve --bg http://127.0.0.1:<port>` puts the Angler at https://<pc>.<tailnet>.ts.net/
with a real certificate, while the Angler itself stays plain HTTP. `baitbook phone` sets it up;
the PC app's Settings and `baitbook status` show the address. Everything here asks the
`tailscale` command, and answers None when it isn't installed, isn't running or doesn't answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DOWNLOAD = "https://tailscale.com/download"
ADMIN_DNS = "https://login.tailscale.com/admin/dns"
# where the Windows and macOS apps put the command when it isn't on PATH
APP_PATHS = (r"C:\Program Files\Tailscale\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def exe() -> str | None:
    """The `tailscale` command: on PATH, or where the Windows or macOS app installed it."""
    return shutil.which("tailscale") or next((p for p in APP_PATHS if Path(p).is_file()), None)


def ask(*args: str, timeout: float = 3) -> dict | None:
    """`tailscale <args> --json`, parsed; None if it can't be run or doesn't answer with JSON."""
    ts = exe()
    if not ts:
        return None
    try:
        r = subprocess.run([ts, *args, "--json"], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console flashing up behind the app
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@dataclass
class Machine:
    running: bool  # signed in and connected
    name: str      # this machine's MagicDNS name, "pc.tailnet.ts.net" ("" with MagicDNS off)
    https: bool    # the tailnet hands out certificates (HTTPS Certificates on the admin console's DNS page)


def machine() -> Machine | None:
    """This machine on its tailnet, from `tailscale status`; None if Tailscale doesn't answer."""
    st = ask("status")
    if st is None:
        return None
    return Machine(running=st.get("BackendState") == "Running", name=((st.get("Self") or {}).get("DNSName") or "").rstrip("."),
                   https=bool(st.get("CertDomains")))


def serve_config() -> dict | None:
    """`tailscale serve status`: {} when nothing is served, None if Tailscale doesn't answer."""
    return ask("serve", "status")


def _routes(cfg: dict):
    """(host:port, mount point, proxy target) for every web handler, the background config's and
    any foreground `tailscale serve` session's."""
    for c in [cfg, *(cfg.get("Foreground") or {}).values()]:
        for hostport, web in (c.get("Web") or {}).items():
            for mount, handler in ((web or {}).get("Handlers") or {}).items():
                yield hostport, mount, (handler or {}).get("Proxy") or ""


def _is_angler(proxy: str, port: int) -> bool:
    """Does a proxy target ("http://127.0.0.1:8787", "localhost:8787", ...) name this port on loopback?"""
    u = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    try:
        return u.scheme == "http" and u.hostname in LOOPBACK and u.port == port
    except ValueError:  # a port that isn't a number
        return False


def _url(hostport: str) -> str:
    host, _, p = hostport.rpartition(":")
    return f"https://{host}{'' if p == '443' else ':' + p}/device/"


def serve_url(port: int, cfg: dict | None = None) -> str | None:
    """The phone app's address through `tailscale serve`, if it passes the root of an HTTPS address
    on to this Angler (plain HTTP on 127.0.0.1:port); None if not, or if Tailscale doesn't answer.
    The app asks for /api/..., /shared/... from the root, so a handler mounted at a path won't do."""
    cfg = serve_config() if cfg is None else cfg
    for hostport, mount, proxy in _routes(cfg or {}):
        if mount == "/" and _is_angler(proxy, port):
            return _url(hostport)
    return None


def root_taken(name: str, port: int, cfg: dict) -> str | None:
    """What `tailscale serve` already passes https://<name>/ to, when that isn't this Angler:
    serving the Angler there would replace it."""
    for hostport, mount, proxy in _routes(cfg):
        if hostport == f"{name}:443" and mount == "/" and not _is_angler(proxy, port):
            return proxy or "something else"
    return None
