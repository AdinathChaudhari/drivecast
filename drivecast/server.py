"""FastAPI application: API routes, streaming proxy, static UI."""
import asyncio
import logging
import os
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
        self.library = Library()
        self.library.seed_api_cache(self.api)
        self.scanner = Scanner(self.api, self.tmdb, self.library,
                               throttle=cfg.get("scan_throttle", 0.15))
        port = cfg.get("port", 8737)
        self.player = PlayerManager(cfg, self.history, "http://127.0.0.1:%d" % port)
        self.setup_error = None  # populated by preflight if rclone is unusable
        self._refresh_task = None

    def start_refresh(self):
        """Kick a background library scan of the selected drives, if idle.

        Returns True if a scan was started, False if one is already running or
        no drives are selected.
        """
        drives = self.cfg.get("selected_drives") or []
        if not drives or self.scanner.status.get("running"):
            return False
        self._refresh_task = asyncio.create_task(self.scanner.scan(drives))
        return True

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
    async def api_refresh():
        state = app.state.dc
        if state.setup_error:
            return JSONResponse({"error": "setup", "message": state.setup_error}, status_code=503)
        if state.scanner.status.get("running"):
            return {"started": False, "running": True}
        if not (state.cfg.get("selected_drives") or []):
            return JSONResponse(
                {"error": "no_drives", "message": "No drives selected. Pick drives in Settings."},
                status_code=400,
            )
        state.start_refresh()
        return {"started": True, "running": True}

    @app.get("/api/refresh/status")
    async def api_refresh_status():
        state = app.state.dc
        return dict(state.scanner.status)

    @app.get("/api/settings")
    async def api_get_settings():
        state = app.state.dc
        from .player import detect_player
        available = [k for k in ("mpv", "iina", "vlc") if detect_player(k)[0]]
        return {
            "selected_drives": state.cfg.get("selected_drives", []),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
            "player": state.cfg.get("player", "auto"),
            "available_players": available,
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
        if "auto_refresh_on_startup" in body:
            state.cfg["auto_refresh_on_startup"] = bool(body.get("auto_refresh_on_startup"))
        if "autoplay_next" in body:
            state.cfg["autoplay_next"] = bool(body.get("autoplay_next"))
        if "player" in body:
            choice = str(body.get("player") or "auto")
            if choice in ("auto", "mpv", "iina", "vlc"):
                state.cfg["player"] = choice
        config_mod.save_config(state.cfg)
        started = False
        if drives_changed:
            started = state.start_refresh()
        return {
            "ok": True,
            "selected_drives": state.cfg.get("selected_drives", []),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
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
        body = await request.json()
        file_id = body.get("file_id")
        name = body.get("name") or file_id
        if not file_id:
            return JSONResponse({"error": "bad_request", "message": "file_id required"}, status_code=400)
        duration_ms = body.get("duration_ms")
        # Prefer the library cache for duration so playback launches without a
        # blocking Drive metadata call.
        if not duration_ms:
            info = state.library.file_info(file_id)
            if info:
                duration_ms = info.get("duration_ms")
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
                qdur = item.get("duration_ms")
                if not qdur:
                    info = state.library.file_info(fid)
                    if info:
                        qdur = info.get("duration_ms")
                queue.append({
                    "file_id": fid,
                    "name": item.get("name") or fid,
                    "duration_ms": qdur,
                })
        if body.get("start_over"):
            # Clear the saved resume position so the player starts at 0.
            state.history.update(file_id, name=name, drive_id=drive_id,
                                 parent_id=parent_id, position=0.0, force=True)
        try:
            result = state.player.play(
                file_id, name, duration_ms=duration_ms,
                drive_id=drive_id, parent_id=parent_id, queue=queue,
            )
            return result
        except PlayerError as e:
            return JSONResponse({"error": "no_player", "message": str(e)}, status_code=501)

    @app.get("/api/continue")
    async def api_continue():
        state = app.state.dc
        return {"items": state.history.continue_watching()}

    @app.get("/api/watched-map")
    async def api_watched_map():
        """file_id -> last_played epoch, for the client-side "Recently watched" sort."""
        state = app.state.dc
        return {"map": state.history.last_played_map()}

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

    # ---- static assets ----
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


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
