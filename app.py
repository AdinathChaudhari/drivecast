#!/usr/bin/env python3
"""drivecast entry point.

Preflight-checks rclone, starts uvicorn on 127.0.0.1:<port>, and opens the
browser (unless DRIVECAST_NO_BROWSER=1).
"""
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from drivecast import config as config_mod
from drivecast.player import detect_player
from drivecast.rclone_auth import RcloneError, TokenManager
from drivecast.server import create_app


def _port_is_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preflight_rclone(cfg):
    """Non-fatal preflight: warn if rclone can't produce a token.

    We still start the server (it shows a friendly setup page) but print a
    clear console message.
    """
    tm = TokenManager(cfg["remote"])
    import asyncio
    try:
        asyncio.run(tm.get_token())
        print("[drivecast] rclone remote '%s' OK." % cfg["remote"])
    except RcloneError as e:
        print("[drivecast] WARNING: %s" % e, file=sys.stderr)
        print("[drivecast] Starting anyway; the web UI will show setup instructions.",
              file=sys.stderr)


def _report_player(cfg):
    kind, path = detect_player(cfg.get("player", "auto"))
    if kind == "mpv":
        print("[drivecast] Player: mpv (%s) — resume tracking enabled." % path)
    elif kind == "iina":
        print("[drivecast] Player: IINA (%s) — resume tracking enabled." % path)
    elif kind == "vlc":
        print("[drivecast] Player: VLC (%s) — no resume tracking; install mpv for that "
              "(brew install mpv)." % path)
    else:
        print("[drivecast] WARNING: no player found. Install mpv: brew install mpv",
              file=sys.stderr)


def _open_browser_later(url):
    if os.environ.get("DRIVECAST_NO_BROWSER") == "1":
        return

    def _open():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Line-buffer stdout/stderr so console messages appear promptly even when
    # output is redirected to a file/pipe.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    cfg = config_mod.load_config()
    host = "127.0.0.1"
    port = int(cfg.get("port", 8737))

    if not _port_is_free(host, port):
        print("[drivecast] ERROR: port %d on %s is already in use. "
              "Change \"port\" in config.json or stop the other process."
              % (port, host), file=sys.stderr)
        sys.exit(1)

    _preflight_rclone(cfg)
    _report_player(cfg)

    url = "http://%s:%d/" % (host, port)
    print("[drivecast] Serving at %s" % url)
    _open_browser_later(url)

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
