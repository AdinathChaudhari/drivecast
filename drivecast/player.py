"""External player launch + resume tracking.

Player preference: mpv (on PATH) -> IINA (iina-cli) -> VLC (direct binary).
We never use `open -a`; we invoke the binary directly so we can pass flags and
know when it exits.

mpv / IINA expose a JSON IPC socket we poll for playback-time so we can save
resume positions. VLC has no such tracking here — it resumes to the saved
position via --start-time and we keep the prior position untouched.
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

log = logging.getLogger("drivecast.player")

IINA_CLI = "/Applications/IINA.app/Contents/MacOS/iina-cli"
VLC_BIN = "/Applications/VLC.app/Contents/MacOS/VLC"

SOCKET_WAIT_SECONDS = 10.0
POLL_INTERVAL = 3.0

# Network buffering + hardware decode flags: the file is streamed over HTTP from
# Drive, so a generous demuxer cache + readahead hides latency and hiccups, and
# hw decode keeps 4K smooth. These are the biggest playback-speed win.
MPV_CACHE_FLAGS = [
    "--cache=yes",
    "--cache-secs=30",
    "--demuxer-max-bytes=150MiB",
    "--demuxer-max-back-bytes=50MiB",
    "--demuxer-readahead-secs=20",
    "--hwdec=auto-safe",
    "--force-seekable=yes",
    "--network-timeout=30",
]
# IINA takes the same options prefixed with --mpv-.
IINA_CACHE_FLAGS = ["--mpv-" + flag[2:] for flag in MPV_CACHE_FLAGS]


def build_mpv_args(path, sock, resume, name, url):
    """Construct the mpv command line (IPC + resume + title + cache/hwdec)."""
    return [
        path,
        "--input-ipc-server=%s" % sock,
        "--start=%d" % int(resume),
        "--force-media-title=%s" % name,
        "--no-terminal",
        *MPV_CACHE_FLAGS,
        url,
    ]


def build_iina_args(path, sock, resume, name, url):
    """Construct the IINA command line (mpv-prefixed IPC + resume + cache/hwdec)."""
    return [
        path,
        "--mpv-input-ipc-server=%s" % sock,
        "--mpv-start=%d" % int(resume),
        "--mpv-force-media-title=%s" % name,
        *IINA_CACHE_FLAGS,
        url,
    ]


def detect_player(preference="auto"):
    """Return (kind, path) where kind in {"mpv","iina","vlc"} or (None, None)."""
    if preference in ("mpv", "iina", "vlc"):
        order = [preference]
    else:
        order = ["mpv", "iina", "vlc"]
    for kind in order:
        if kind == "mpv":
            path = shutil.which("mpv")
            if path:
                return "mpv", path
        elif kind == "iina":
            if os.path.exists(IINA_CLI):
                return "iina", IINA_CLI
        elif kind == "vlc":
            if os.path.exists(VLC_BIN):
                return "vlc", VLC_BIN
    return None, None


class PlayerError(Exception):
    pass


class PlayerManager:
    def __init__(self, cfg, history, base_url):
        self.cfg = cfg
        self.history = history
        self.base_url = base_url.rstrip("/")
        self._session = None  # dict for the currently tracked session
        self._session_lock = threading.Lock()

    def stream_url(self, file_id):
        return "%s/stream/%s" % (self.base_url, file_id)

    def play(self, file_id, name, duration_ms=None, drive_id=None, parent_id=None):
        """Launch the configured player at the resume position.

        Returns {"player": kind, "resumed_from": seconds}.
        """
        kind, path = detect_player(self.cfg.get("player", "auto"))
        if not kind:
            raise PlayerError(
                "No supported player found. Install mpv for best results: brew install mpv"
            )

        resume = self.history.resume_position(file_id)
        url = self.stream_url(file_id)
        duration = (float(duration_ms) / 1000.0) if duration_ms else None

        # Stop any prior tracked poller before starting a new session.
        self._stop_current_session()

        if kind == "mpv":
            self._launch_mpv(path, file_id, name, url, resume, duration, drive_id, parent_id)
        elif kind == "iina":
            self._launch_iina(path, file_id, name, url, resume, duration, drive_id, parent_id)
        else:  # vlc
            self._launch_vlc(path, file_id, name, url, resume, drive_id, parent_id)

        return {"player": kind, "resumed_from": resume}

    # ---- launchers ----

    def _launch_mpv(self, path, file_id, name, url, resume, duration, drive_id, parent_id):
        sock = "/tmp/drivecast-%s.sock" % uuid.uuid4().hex[:12]
        args = build_mpv_args(path, sock, resume, name, url)
        proc = subprocess.Popen(args)
        self._start_poller("mpv", proc, sock, file_id, name, duration, drive_id, parent_id)

    def _launch_iina(self, path, file_id, name, url, resume, duration, drive_id, parent_id):
        sock = "/tmp/drivecast-%s.sock" % uuid.uuid4().hex[:12]
        args = build_iina_args(path, sock, resume, name, url)
        proc = subprocess.Popen(args)
        self._start_poller("iina", proc, sock, file_id, name, duration, drive_id, parent_id)

    def _launch_vlc(self, path, file_id, name, url, resume, drive_id, parent_id):
        args = [
            path,
            "--start-time=%d" % int(resume),
            "--play-and-exit",
            "--meta-title", name,
            url,
        ]
        subprocess.Popen(args)
        # VLC: no IPC tracking. Just record the play timestamp, keep prior position.
        self.history.update(file_id, name=name, drive_id=drive_id,
                            parent_id=parent_id, force=True)

    # ---- IPC poller (mpv / IINA) ----

    def _stop_current_session(self):
        with self._session_lock:
            sess = self._session
            self._session = None
        if sess:
            sess["stop"].set()

    def _start_poller(self, kind, proc, sock, file_id, name, duration, drive_id, parent_id):
        stop = threading.Event()
        session = {"stop": stop, "sock": sock, "proc": proc, "file_id": file_id}
        with self._session_lock:
            self._session = session
        t = threading.Thread(
            target=self._poll_loop,
            args=(kind, proc, sock, file_id, name, duration, drive_id, parent_id, stop),
            daemon=True,
        )
        t.start()

    def _poll_loop(self, kind, proc, sock, file_id, name, duration, drive_id, parent_id, stop):
        # Seed an entry so it appears immediately / carries metadata.
        self.history.update(file_id, name=name, drive_id=drive_id,
                            parent_id=parent_id, duration=duration, force=True)

        conn = self._wait_for_socket(sock, stop)
        if conn is None:
            log.debug("%s IPC socket never appeared for %s; launch-only mode", kind, file_id)
            self._cleanup_socket(sock)
            return

        last_pos = 0.0
        got_duration = duration
        try:
            while not stop.is_set():
                if proc.poll() is not None:
                    break  # player exited
                pos = self._ipc_get(conn, "playback-time")
                if pos is not None:
                    last_pos = float(pos)
                    if got_duration is None:
                        d = self._ipc_get(conn, "duration")
                        if d:
                            got_duration = float(d)
                    self.history.update(file_id, name=name, drive_id=drive_id,
                                        parent_id=parent_id, position=last_pos,
                                        duration=got_duration)
                # Wait POLL_INTERVAL but wake early if asked to stop.
                if stop.wait(POLL_INTERVAL):
                    break
        except Exception as exc:
            log.debug("poller error for %s: %r", file_id, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            # Final save: mark watched if we reached the end.
            final_dur = got_duration or 0.0
            if final_dur and last_pos >= 0.9 * final_dur:
                self.history.update(file_id, name=name, drive_id=drive_id,
                                    parent_id=parent_id, position=last_pos,
                                    duration=final_dur, force=True)
                self.history.mark_watched(file_id, True)
            else:
                self.history.update(file_id, name=name, drive_id=drive_id,
                                    parent_id=parent_id, position=last_pos,
                                    duration=got_duration, force=True)
            self._cleanup_socket(sock)

    def _wait_for_socket(self, sock, stop, timeout=SOCKET_WAIT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline and not stop.is_set():
            if os.path.exists(sock):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(2.0)
                    s.connect(sock)
                    return s
                except OSError:
                    pass
            if stop.wait(0.25):
                return None
        return None

    def _ipc_get(self, conn, prop):
        """Send a get_property command and return its value, or None."""
        cmd = json.dumps({"command": ["get_property", prop]}) + "\n"
        try:
            conn.sendall(cmd.encode("utf-8"))
            conn.settimeout(2.0)
            buf = b""
            # mpv may emit event lines; read until we see a line with our result.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if "error" in msg and "data" in msg:
                        if msg.get("error") == "success":
                            return msg.get("data")
                        return None
            return None
        except OSError:
            return None

    def _cleanup_socket(self, sock):
        try:
            if os.path.exists(sock):
                os.unlink(sock)
        except OSError:
            pass
