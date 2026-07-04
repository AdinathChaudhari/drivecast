"""FastAPI application: API routes, streaming proxy, static UI."""
import asyncio
import hmac
import io
import ipaddress
import logging
import os
import secrets
import socket
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import naming
from .drive_api import DriveAPI, DriveAPIError
from .history import History
from .library import Library, Scanner
from .player import PlayerError, PlayerManager, detect_player
from .rclone_auth import RcloneError, TokenManager
from .scan_cache import ScanCache
from .subtitles import SubtitleResolver
from .streaming import Streamer
from .tmdb import TMDB

log = logging.getLogger("drivecast.server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class AppState:
    """Container for all long-lived services."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tokens = TokenManager(cfg["remote"])
        self.api = DriveAPI(self.tokens, self.tokens.list_drives)
        self.streamer = Streamer(self.tokens, self.api)
        self.history = History()
        self.tmdb = TMDB(cfg.get("tmdb_api_key"))
        self.library = Library(drive_sections=cfg.get("drive_sections") or {})
        self.library.seed_api_cache(self.api)
        self.scanner = Scanner(self.api, self.tmdb, self.library,
                               throttle=cfg.get("scan_throttle", 0.15),
                               cache=ScanCache())
        port = cfg.get("port", 8737)
        self.player = PlayerManager(cfg, self.history, "http://127.0.0.1:%d" % port)
        self.subtitles = SubtitleResolver(self.api, cfg.get("opensubtitles_api_key"))
        # Autoplay-advanced episodes reuse already-cached subtitles (sync).
        self.player.sub_cache_lookup = self.subtitles.cached
        self.setup_error = None  # populated by preflight if rclone is unusable
        self._refresh_task = None
        self._pending_scope = set()  # scopes requested while a scan was running

    def start_refresh(self, scope=None):
        """Kick a background library scan, if idle.

        `scope` optionally limits the Drive re-walk to specific selected
        drives (per-drive refresh); the library is still rebuilt over all
        selected drives from the scan cache. Returns True if a scan started.
        A scoped request that arrives while a scan is running is queued and
        re-kicked when the running scan finishes (a section change saved
        mid-scan must not be silently dropped).
        """
        drives = self.cfg.get("selected_drives") or []
        scope = [d for d in (scope or drives) if d in drives]
        if not scope:
            return False
        if self.scanner.status.get("running"):
            self._pending_scope.update(scope)
            return False
        if self._pending_scope:
            scope = sorted(set(scope) | self._pending_scope)
            self._pending_scope.clear()
        self._refresh_task = asyncio.create_task(self.scanner.scan(
            drives, scope=scope,
            drive_hints=self.cfg.get("drive_hints") or {},
            drive_sections=self.cfg.get("drive_sections") or {}))
        self._refresh_task.add_done_callback(self._drain_pending_scope)
        return True

    def _drain_pending_scope(self, _task):
        """After a scan finishes, run any refresh that was requested meanwhile."""
        if not self._pending_scope:
            return
        pending = sorted(self._pending_scope)
        self._pending_scope.clear()
        self.start_refresh(scope=pending)

    def maybe_autorefresh(self):
        """On startup: rescan if configured to, or if we have drives but no cache."""
        if self.setup_error:
            return
        drives = self.cfg.get("selected_drives") or []
        if not drives:
            return
        if self.cfg.get("auto_refresh_on_startup") or self.library.is_empty():
            self.start_refresh()

    async def preflight(self):
        """Verify rclone can produce a token; record a friendly error if not."""
        try:
            await self.tokens.get_token()
            self.setup_error = None
        except RcloneError as e:
            self.setup_error = str(e)
            log.warning("Preflight failed: %s", e)

    async def aclose(self):
        try:
            self.history.flush()
        except Exception:
            pass
        await self.api.aclose()
        await self.streamer.aclose()
        await self.tmdb.aclose()
        await self.subtitles.aclose()


def _drive_error_response(e):
    status = 404 if e.status == 404 else (403 if e.status == 403 else 502)
    return JSONResponse(
        {"error": "drive_api", "status": e.status, "reason": e.reason, "message": e.message},
        status_code=status,
    )


def create_app(cfg=None):
    cfg = cfg or config_mod.load_config()

    @asynccontextmanager
    async def lifespan(app):
        state = AppState(cfg)
        app.state.dc = state
        await state.preflight()
        state.maybe_autorefresh()
        yield
        await state.aclose()

    app = FastAPI(title="drivecast", lifespan=lifespan)

    # ---- remote access (opt-in) ----
    # Loopback is trusted (the socket peer, not a spoofable header). "testclient"
    # is starlette's TestClient seam and is likewise a socket-level identity.
    LOCAL_HOSTS = ("127.0.0.1", "::1", "testclient")

    def _client_is_local(request):
        client = request.client
        return bool(client) and client.host in LOCAL_HOSTS

    @app.middleware("http")
    async def _remote_auth(request: Request, call_next):
        if _client_is_local(request):
            return await call_next(request)
        if not cfg.get("remote_access"):
            return JSONResponse({"error": "remote_disabled"}, status_code=403)
        cookie_token = request.cookies.get("dc_token")
        query_token = request.query_params.get("token")
        token = cookie_token or query_token
        cfg_token = cfg.get("remote_token") or ""
        # Constant-time compare; an empty configured token never authorizes.
        # Bytes compare: constant-time AND tolerant of non-ASCII client input
        # (str compare_digest raises TypeError on non-ASCII, a pre-auth 500).
        ok = (bool(cfg_token) and bool(token)
              and hmac.compare_digest(str(token).encode("utf-8"),
                                      str(cfg_token).encode("utf-8")))
        if not ok:
            if "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(_token_page(), status_code=401)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        # A ?token= link bootstraps a durable browser session so later
        # same-origin requests (the <video> src, static assets) carry the cookie.
        if not cookie_token and query_token:
            response.set_cookie("dc_token", query_token, httponly=True,
                                samesite="lax", max_age=180 * 24 * 3600)
        return response

    # ---- setup / preflight gate ----

    @app.get("/", response_class=HTMLResponse)
    async def index():
        state = app.state.dc
        if state.setup_error:
            return HTMLResponse(_setup_page(state.setup_error))
        # Read as UTF-8 explicitly: inside a packaged .app the process locale can
        # default to ASCII, which would choke on the page's unicode glyphs.
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # ---- API ----

    @app.get("/api/drives")
    async def api_drives():
        state = app.state.dc
        if state.setup_error:
            return JSONResponse({"error": "setup", "message": state.setup_error}, status_code=503)
        try:
            return {"drives": await state.api.list_drives()}
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    @app.get("/api/browse")
    async def api_browse(drive_id: str, folder_id: str = None, page_token: str = None):
        state = app.state.dc
        try:
            res = await state.api.browse(
                drive_id, folder_id, page_token, page_size=cfg.get("page_size", 200)
            )
            return res
        except DriveAPIError as e:
            return _drive_error_response(e)
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    @app.get("/api/search")
    async def api_search(q: str, page_token: str = None):
        state = app.state.dc
        if not q.strip():
            return {"files": [], "nextPageToken": None}
        try:
            return await state.api.search(q, page_token, page_size=cfg.get("page_size", 200))
        except DriveAPIError as e:
            return _drive_error_response(e)
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    # ---- library ----

    @app.get("/api/library")
    async def api_library():
        state = app.state.dc
        return {
            "titles": state.library.titles_list(),
            "generated_at": state.library.generated_at(),
            "scanning": bool(state.scanner.status.get("running")),
            "selected_drives": state.cfg.get("selected_drives", []),
        }

    @app.get("/api/title/{title_id}")
    async def api_title(title_id: str):
        state = app.state.dc
        rec = state.library.get(title_id)
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return rec

    @app.post("/api/refresh")
    async def api_refresh(request: Request):
        state = app.state.dc
        if state.setup_error:
            return JSONResponse({"error": "setup", "message": state.setup_error}, status_code=503)
        if state.scanner.status.get("running"):
            return {"started": False, "running": True}
        selected = state.cfg.get("selected_drives") or []
        if not selected:
            return JSONResponse(
                {"error": "no_drives", "message": "No drives selected. Pick drives in Settings."},
                status_code=400,
            )
        # Optional body {"drives": [...]} scopes the refresh to those drives
        # (must be selected). No/empty body = full refresh (menubar compat).
        try:
            body = await request.json()
        except Exception:
            body = {}
        drives = body.get("drives") if isinstance(body, dict) else None
        if drives is not None and not isinstance(drives, list):
            return JSONResponse({"error": "bad_request", "message": "drives must be a list"},
                                status_code=400)
        if drives:
            unknown = [d for d in drives if d not in selected]
            if unknown:
                return JSONResponse(
                    {"error": "bad_request",
                     "message": "Not a selected drive: %s" % ", ".join(unknown)},
                    status_code=400,
                )
        started = state.start_refresh(scope=drives or None)
        return {"started": started, "running": True, "scope": drives or selected}

    @app.get("/api/refresh/status")
    async def api_refresh_status():
        state = app.state.dc
        st = dict(state.scanner.status)
        scope = st.get("scope") or []
        names = {}
        if scope:
            try:
                names = {d["id"]: d.get("name") for d in await state.api.list_drives()}
            except Exception:
                pass
        st["scope_names"] = [names.get(d) or d for d in scope]
        return st

    @app.get("/api/sections")
    async def api_sections():
        """Section metadata for the UI: built-ins plus any custom private
        section plugins (see sections.py)."""
        from . import sections as sections_mod
        return {"sections": sections_mod.meta_list()}

    @app.get("/api/settings")
    async def api_get_settings():
        state = app.state.dc
        from .player import detect_player
        available = [k for k in ("mpv", "iina", "vlc") if detect_player(k)[0]]
        return {
            "selected_drives": state.cfg.get("selected_drives", []),
            "drive_sections": state.cfg.get("drive_sections", {}),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
            "subtitles": bool(state.cfg.get("subtitles", True)),
            "player": state.cfg.get("player", "auto"),
            "available_players": available,
            "remote_access": bool(state.cfg.get("remote_access", False)),
        }

    @app.post("/api/settings")
    async def api_post_settings(request: Request):
        state = app.state.dc
        body = await request.json()
        drives_changed = False
        if "selected_drives" in body:
            new_drives = list(body.get("selected_drives") or [])
            if new_drives != (state.cfg.get("selected_drives") or []):
                drives_changed = True
            state.cfg["selected_drives"] = new_drives
        # Section assignments: validate values, refresh just the drives whose
        # section changed (their content must be re-classified).
        section_changed = []
        if "drive_sections" in body:
            from . import sections as sections_mod
            raw = body.get("drive_sections")
            if not isinstance(raw, dict):
                raw = {}
            selected_now = state.cfg.get("selected_drives") or []
            valid = sections_mod.all_sections()
            # Only selected drives can carry an assignment — a stale entry for
            # an unchecked drive would keep an empty section tab alive.
            new_sections = {k: v for k, v in raw.items()
                            if isinstance(k, str) and v in valid
                            and k in selected_now}
            old_sections = state.cfg.get("drive_sections") or {}
            section_changed = [
                d for d in selected_now
                if (old_sections.get(d) or "entertainment")
                != (new_sections.get(d) or "entertainment")
            ]
            state.cfg["drive_sections"] = new_sections
        if "auto_refresh_on_startup" in body:
            state.cfg["auto_refresh_on_startup"] = bool(body.get("auto_refresh_on_startup"))
        if "autoplay_next" in body:
            state.cfg["autoplay_next"] = bool(body.get("autoplay_next"))
        if "subtitles" in body:
            state.cfg["subtitles"] = bool(body.get("subtitles"))
        if "player" in body:
            choice = str(body.get("player") or "auto")
            if choice in ("auto", "mpv", "iina", "vlc"):
                state.cfg["player"] = choice
        # Remote access: a change to the bind mode only takes effect on the next
        # launch, so flag restart_required when it flips. Enabling it mints a
        # token if none exists yet (persisted like any other non-secret setting).
        restart_required = False
        if "remote_access" in body:
            new_remote = bool(body.get("remote_access"))
            if new_remote != bool(state.cfg.get("remote_access", False)):
                restart_required = True
            state.cfg["remote_access"] = new_remote
            if new_remote and not state.cfg.get("remote_token"):
                state.cfg["remote_token"] = secrets.token_urlsafe(16)
        config_mod.save_config(state.cfg)
        started = False
        if drives_changed:
            started = state.start_refresh()
        elif section_changed:
            started = state.start_refresh(scope=section_changed)
        return {
            "ok": True,
            "selected_drives": state.cfg.get("selected_drives", []),
            "drive_sections": state.cfg.get("drive_sections", {}),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
            "subtitles": bool(state.cfg.get("subtitles", True)),
            "remote_access": bool(state.cfg.get("remote_access", False)),
            "restart_required": restart_required,
            "refresh_started": started,
        }

    @app.api_route("/stream/{file_id}", methods=["GET", "HEAD"])
    async def stream(file_id: str, request: Request):
        state = app.state.dc
        if request.method == "HEAD":
            try:
                return await state.streamer.head(file_id)
            except DriveAPIError as e:
                return _drive_error_response(e)
        return await state.streamer.stream(file_id, request)

    @app.post("/api/play")
    async def api_play(request: Request):
        state = app.state.dc
        # A phone must never launch mpv on the Mac — it uses the web player.
        if not _client_is_local(request):
            return JSONResponse(
                {"error": "local_only",
                 "message": "Playback from this device uses the web player."},
                status_code=403,
            )
        body = await request.json()
        file_id = body.get("file_id")
        name = body.get("name") or file_id
        if not file_id:
            return JSONResponse({"error": "bad_request", "message": "file_id required"}, status_code=400)
        duration_ms = body.get("duration_ms")
        # Prefer the library cache for duration so playback launches without a
        # blocking Drive metadata call — and for the media kind, so audio
        # files play windowed no matter which UI path launched them
        # (Continue shelf and movie tiles don't send media).
        info = state.library.file_info(file_id)
        if not duration_ms and info:
            duration_ms = info.get("duration_ms")
        media = body.get("media") or (info or {}).get("media")
        drive_id = body.get("drive_id")
        parent_id = body.get("parent_id")
        # Optional autoplay queue: episodes to play AFTER this one. Whitelist the
        # fields and drop anything malformed (non-list / items without a file_id).
        queue = []
        raw_queue = body.get("queue")
        if isinstance(raw_queue, list):
            for item in raw_queue:
                if not isinstance(item, dict):
                    continue
                fid = item.get("file_id")
                if not fid:
                    continue
                qinfo = state.library.file_info(fid)
                qdur = item.get("duration_ms")
                if not qdur and qinfo:
                    qdur = qinfo.get("duration_ms")
                qmedia = item.get("media") or (qinfo or {}).get("media")
                queue.append({
                    "file_id": fid,
                    "name": item.get("name") or fid,
                    "duration_ms": qdur,
                    "media": "audio" if qmedia == "audio" else None,
                })
        if body.get("start_over"):
            # Clear the saved resume position so the player starts at 0.
            state.history.update(file_id, name=name, drive_id=drive_id,
                                 parent_id=parent_id, position=0.0, force=True)
        # English subtitles (cached / sibling .srt / OpenSubtitles) — bounded
        # so a slow lookup can only delay playback, never block it.
        sub_path = None
        if state.cfg.get("subtitles", True) and media != "audio":
            try:
                sub_path = await asyncio.wait_for(
                    state.subtitles.resolve(file_id, name, drive_id, parent_id),
                    timeout=10.0)
            except Exception:
                sub_path = None
        try:
            result = state.player.play(
                file_id, name, duration_ms=duration_ms,
                drive_id=drive_id, parent_id=parent_id, queue=queue,
                media="audio" if media == "audio" else None,
                sub_path=sub_path,
            )
            result["subtitles"] = bool(sub_path)
            return result
        except PlayerError as e:
            return JSONResponse({"error": "no_player", "message": str(e)}, status_code=501)

    @app.get("/api/continue")
    async def api_continue():
        state = app.state.dc
        items = state.history.continue_watching()
        # Enrich each item with its owning library title so the shelf can show
        # the poster and a clean display name instead of the raw filename.
        for item in items:
            rec = state.library.title_for_file(item.get("file_id"))
            if rec:
                item["title"] = rec.get("title")
                item["title_id"] = rec.get("id")
                item["type"] = rec.get("type")
                item["poster"] = rec.get("poster")
                item["section"] = rec.get("section", "entertainment")
        return {"items": items}

    @app.get("/api/watched-map")
    async def api_watched_map():
        """file_id -> last_played epoch, for the client-side "Recently watched"
        sort; plus per-file progress for course/episode completion UI."""
        state = app.state.dc
        return {"map": state.history.last_played_map(),
                "progress": state.history.progress_map()}

    @app.post("/api/enrich")
    async def api_enrich(request: Request):
        """Batch parse filenames and (optionally) enrich with TMDB."""
        state = app.state.dc
        body = await request.json()
        names = body.get("names") or []
        out = {}
        for raw in names:
            parsed = naming.parse(raw)
            entry = {
                "title": parsed["title"],
                "year": parsed["year"],
                "type": parsed["type"],
                "season": parsed["season"],
                "episode": parsed["episode"],
                "poster_key": None,
            }
            if state.tmdb.enabled:
                meta = await state.tmdb.enrich(parsed["title"], parsed["year"], parsed["type"])
                if meta:
                    entry["poster_key"] = meta.get("poster_key")
                    if meta.get("year"):
                        entry["year"] = meta["year"]
            out[raw] = entry
        return {"results": out}

    @app.get("/api/poster/{key:path}")
    async def api_poster(key: str, request: Request):
        """Serve a cached TMDB poster, or proxy a Drive thumbnailLink fallback."""
        state = app.state.dc
        # thumbnailLink fallback: key is passed as ?thumb=<url>
        thumb = request.query_params.get("thumb")
        local = state.tmdb.poster_path(key) if key and key != "_" else None
        if local:
            return FileResponse(local, media_type="image/jpeg")
        if thumb:
            try:
                import httpx
                tok = await state.tokens.get_token()
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.get(thumb, headers={"Authorization": "Bearer %s" % tok})
                if r.status_code == 200:
                    return Response(
                        content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"),
                    )
            except Exception:
                pass
        return JSONResponse({"error": "not_found"}, status_code=404)

    # ---- remote access endpoints ----

    @app.get("/api/remote")
    async def api_remote():
        """Connection info for the phone. Only reachable through the middleware,
        so any authorized caller may see the token (it's already in their URL)."""
        state = app.state.dc
        port = int(state.cfg.get("port", 8737))
        token = state.cfg.get("remote_token") or ""
        urls = []
        # Tailscale first (works from anywhere, encrypted), then LAN.
        ts_ip = _tailscale_ip()
        if ts_ip:
            urls.append({"label": "Tailscale", "url": _remote_url(ts_ip, port, token)})
        lan_ip = _lan_ip()
        if lan_ip:
            urls.append({"label": "Wi-Fi", "url": _remote_url(lan_ip, port, token)})
        return {
            "enabled": bool(state.cfg.get("remote_access")),
            "token": token,
            "port": port,
            "urls": urls,
        }

    @app.get("/api/remote/qr")
    async def api_remote_qr():
        state = app.state.dc
        if not state.cfg.get("remote_access"):
            return JSONResponse({"error": "remote_disabled"}, status_code=404)
        port = int(state.cfg.get("port", 8737))
        token = state.cfg.get("remote_token") or ""
        ip = _tailscale_ip() or _lan_ip()
        if not ip:
            return JSONResponse({"error": "no_url"}, status_code=404)
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(_remote_url(ip, port, token),
                          image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    @app.post("/api/progress")
    async def api_progress(request: Request):
        """The web player reports watch progress here so Continue Watching,
        resume and watched state stay in sync across devices."""
        state = app.state.dc
        body = await request.json()
        file_id = body.get("file_id")
        if not file_id:
            return JSONResponse({"error": "bad_request", "message": "file_id required"},
                                status_code=400)
        raw_dur = body.get("duration")
        state.history.update(
            file_id,
            name=body.get("name"),
            position=float(body.get("position") or 0.0),
            duration=float(raw_dur) if raw_dur else None,
            force=bool(body.get("ended")),
        )
        return {"ok": True}

    # ---- static assets ----
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def _remote_url(ip, port, token):
    return "http://%s:%d/?token=%s" % (ip, port, token)


def _lan_ip():
    """Best-effort LAN IP via the UDP-connect trick. Failure-silent (None)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# Tailscale hands out addresses from the CGNAT range 100.64.0.0/10.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _tailscale_ip():
    """First Tailscale IPv4 (100.64.0.0/10) from the CLI, else None. Silent."""
    for cmd in (["tailscale", "ip", "-4"],
                ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            ip = line.strip()
            if not ip:
                continue
            try:
                if ipaddress.ip_address(ip) in _TAILSCALE_NET:
                    return ip
            except ValueError:
                pass
            break  # only inspect the first non-empty line
    return None


def _token_page():
    """Minimal dark login page (matches the setup-page aesthetic): a GET form
    with a single password field that re-requests "/" with ?token=."""
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>drivecast</title>
<style>
body{background:#0f0f13;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
     display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:360px;width:100%;box-sizing:border-box;padding:32px;background:#17171d;
      border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5)}
h1{margin-top:0;color:#7aa2ff;font-size:20px}
input{width:100%;box-sizing:border-box;padding:12px;margin:12px 0;border-radius:8px;
      border:1px solid #333;background:#000;color:#e8e8ea;font-size:16px}
button{width:100%;padding:12px;border:0;border-radius:8px;background:#7aa2ff;color:#0f0f13;
       font-size:16px;font-weight:600;cursor:pointer}
</style></head>
<body><div class="card">
<h1>drivecast</h1>
<p>Enter the access token to continue.</p>
<form method="get" action="/">
<input type="password" name="token" placeholder="Access token" autofocus>
<button type="submit">Unlock</button>
</form>
</div></body></html>"""


def _setup_page(error):
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>drivecast — setup</title>
<style>
body{background:#0f0f13;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
     display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:640px;padding:40px;background:#17171d;border-radius:16px;
      box-shadow:0 12px 40px rgba(0,0,0,.5)}
h1{margin-top:0;color:#7aa2ff} code{background:#000;padding:2px 6px;border-radius:4px}
pre{background:#000;padding:14px;border-radius:8px;overflow:auto;color:#ff9e9e}
a{color:#7aa2ff}
</style></head>
<body><div class="card">
<h1>drivecast setup needed</h1>
<p>drivecast couldn't get a Google Drive token from rclone:</p>
<pre>%s</pre>
<p>Fix it, then reload:</p>
<ol>
<li>Install rclone: <code>brew install rclone</code></li>
<li>Configure a Google Drive remote (default name <code>gdrive1</code>):
    <code>rclone config</code> &rarr; new remote &rarr; type <b>drive</b> &rarr; full access &rarr; authorise in browser.</li>
<li>Make sure the remote name matches <code>remote</code> in <code>config.json</code>.</li>
<li>Test it: <code>rclone backend drives gdrive1:</code></li>
</ol>
<p>The rclone config must not be encrypted (drivecast reads the token non-interactively).</p>
</div></body></html>""" % (error,)
