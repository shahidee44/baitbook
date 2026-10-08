"""Run the Angler: `baitbook angler` (or `python -m angler`)."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time

import uvicorn

from . import config

log = logging.getLogger("angler")


def lock_data():
    """One Angler per data folder. Returns the open lock file (keep it open), or None if
    another Angler holds it. The OS drops the lock when the process ends, however it ends."""
    f = open(config.DATA / "angler.lock", "a")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def data_in_use() -> bool:
    """Does an Angler hold the data folder? Takes the lock and lets go of it at once."""
    f = lock_data()
    if f is None:
        return True
    if sys.platform == "win32":  # unlocked now, not whenever Windows gets round to it after the close
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    f.close()
    return False


def serve_when_up(server: uvicorn.Server, sock: socket.socket, main: uvicorn.Server) -> None:
    """The loopback listener: answers once the main server's startup has built the engine."""
    while not main.started:
        time.sleep(0.1)
    server.run(sockets=[sock])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="baitbook angler", description="baitbook's engine: vision reads, night shifts, and the API the apps talk to.")
    ap.add_argument("--host", help="override [server] host")
    ap.add_argument("--port", type=int, help="override [server] port")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)  # started by the PC app, with no console
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # the log is UTF-8 even when it goes to a file (data/angler.log)
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # --host / --port hold for this process only, but everything that reads [server] (the phone's
    # addresses, the Tailscale Serve check) has to see them, not the file's
    cfg = config.override({"server": {k: v for k, v in (("host", args.host), ("port", args.port)) if v}})["server"]
    # before anything opens the database: a second Angler would mark the first one's
    # running shift interrupted (Engine._recover) before failing to bind
    lock = lock_data()  # held until the process ends
    if not lock:
        log.warning("Another Angler is already running on %s; this one exits without touching it. "
                    "`baitbook stop` stops that one.", config.DATA)
        sys.exit(1)

    host, port = cfg["host"], cfg["port"]
    ssl = {"ssl_certfile": str(config.path(cfg["ssl_certfile"])), "ssl_keyfile": str(config.path(cfg.get("ssl_keyfile")) or "")} if cfg.get("ssl_certfile") else {}
    listen = [(host, port)]
    if ssl:  # the PC app on this machine gets plain HTTP on loopback: no cert names 127.0.0.1
        listen.append(("127.0.0.1", config.local_port(cfg)))
    socks = []
    for h, p in listen:  # bound here, because uvicorn runs the app's startup before it binds
        try:
            socks.append(socket.create_server((h, p), family=socket.AF_INET6 if ":" in h else socket.AF_INET))
        except OSError as exc:
            log.error("Can't listen on %s:%s (%s). Is something else using that port?", h, p, exc)
            sys.exit(1)
    # where `baitbook status`, `stop` and the PC app find this Angler (config.angler_port): --port may have moved it
    note = config.DATA / "angler.port"
    note.write_text(str(config.local_port(cfg)), encoding="ascii")

    from .server import app, pair_code

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", **ssl))
    servers = [server]
    if ssl:  # lifespan off: the startup (the engine) runs once, in the main server
        local = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        threading.Thread(target=serve_when_up, args=(local, socks[1], server), name="angler-local", daemon=True).start()
        servers.append(local)
    app.state.stop = lambda: [setattr(s, "should_exit", True) for s in servers]  # POST /api/shutdown (`baitbook stop`)
    app.state.background = args.background  # `baitbook` restarts only these when the code changes, never one in a console
    https = f" (HTTPS; the PC app here uses http://127.0.0.1:{listen[1][1]})" if ssl else ""
    log.info("Angler on port %s%s · pairing code %s", port, https, pair_code())
    try:
        server.run(sockets=socks[:1])
    except KeyboardInterrupt:
        pass
    finally:
        note.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
