#!/usr/bin/env python3
"""drivecast_menubar: a macOS menu-bar wrapper around the drivecast server.

Runs the SAME FastAPI app (drivecast.server.create_app) in-process, via uvicorn
in a background daemon thread, and exposes a tiny ☁ menu-bar item:

    drivecast: running on :8737   (status line)
    Open drivecast                -> opens http://127.0.0.1:<port>/
    ----
    Quit                          -> shuts the server down and exits

The server management (ServerThread) and the pre-launch checks are plain
functions/classes so the module stays importable; the rumps-specific UI lives in
`_run_app`, which imports rumps lazily (matching drive-offload's offload_app).

Run directly for a smoke test:
    ./venv/bin/python drivecast_menubar.py
Set DRIVECAST_NO_BROWSER=1 to skip auto-opening the browser (headless testing).
"""
import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from drivecast import config as config_mod
from drivecast.rclone_auth import RcloneError, TokenManager
from drivecast.server import create_app

ICON = "☁"  # ☁


def _port_is_free(host, port):
    """True if we can bind host:port right now (pattern from app.py)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preflight_ok(cfg):
    """True if rclone can currently produce a Drive token.

    Non-fatal: a False result just drives the "setup needed" status line — the
    server still starts and shows its own setup page. Any error (RcloneError or
    otherwise, e.g. a transient rate-limit) counts as "not ready".
    """
    tm = TokenManager(cfg["remote"])
    try:
        asyncio.run(tm.get_token())
        return True
    except RcloneError:
        return False
    except Exception:
        return False


def _open_browser_later(url, delay=1.2):
    """Open the browser once, after a short delay, unless suppressed."""
    if os.environ.get("DRIVECAST_NO_BROWSER") == "1":
        return

    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


class ServerThread:
    """Runs uvicorn.Server(app) in a daemon thread; stop() asks it to exit."""

    def __init__(self, app, host, port):
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self, timeout=5.0):
        """Signal the server to exit and wait (bounded) for the thread."""
        self.server.should_exit = True
        self.thread.join(timeout)


# ==========================================================================
# UI (rumps). Imported lazily so the module above stays importable without it.
# ==========================================================================

def _run_app(server, port, setup_ok, url):
    import rumps

    class DrivecastApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self._server = server
            self._url = url
            status = ("drivecast: running on :%d" % port if setup_ok
                      else "drivecast: setup needed")
            self.status_item = rumps.MenuItem(status)
            self.menu = [
                self.status_item,
                rumps.MenuItem("Open drivecast", callback=self._open),
                None,
                rumps.MenuItem("Quit", callback=self._quit),
            ]

        def _open(self, _sender):
            try:
                webbrowser.open(self._url)
            except Exception:
                pass

        def _quit(self, _sender):
            try:
                self._server.stop()
            except Exception:
                pass
            rumps.quit_application()

    # Open the browser once on launch (after the server has had a moment to bind).
    _open_browser_later(url)
    DrivecastApp().run()


def _notify_already_running(url):
    """Best-effort notification that another instance already holds the port."""
    try:
        import rumps
        rumps.notification(
            "drivecast", "Already running",
            "Opening the existing instance in your browser.")
    except Exception:
        pass


def main():
    cfg = config_mod.load_config()
    host = "127.0.0.1"
    port = int(cfg.get("port", 8737))
    url = "http://%s:%d/" % (host, port)

    # Another instance (or an old app.py) already owns the port: don't crash —
    # point the user at it and bow out.
    if not _port_is_free(host, port):
        _open_browser_later(url, delay=0.0)
        _notify_already_running(url)
        return

    setup_ok = _preflight_ok(cfg)

    app = create_app(cfg)
    server = ServerThread(app, host, port)
    server.start()

    _run_app(server, port, setup_ok, url)


if __name__ == "__main__":
    main()
