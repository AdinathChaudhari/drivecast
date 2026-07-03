"""Google Drive v3 API client (browse / search / metadata).

All calls use a Bearer token from the TokenManager and ALWAYS pass
supportsAllDrives=true / includeItemsFromAllDrives=true so Shared Drives work.
"""
import time

import httpx

FILES_URL = "https://www.googleapis.com/drive/v3/files"

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,videoMediaMetadata,thumbnailLink,parents"
)
LIST_FIELDS = "nextPageToken,files(%s)" % FILE_FIELDS

FOLDER_MIME = "application/vnd.google-apps.folder"

DRIVES_CACHE_TTL = 300  # 5 minutes


def escape_q(value):
    """Escape a value for use inside a Drive query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveAPIError(Exception):
    """A Drive API call returned an error we surface to the caller."""

    def __init__(self, status, message, reason=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason  # google error reason, e.g. "notFound"


class DriveAPI:
    def __init__(self, token_manager, drives_lister):
        """
        token_manager: TokenManager instance.
        drives_lister: async callable returning the raw drives list (from rclone).
        """
        self.tokens = token_manager
        self._drives_lister = drives_lister
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._drives_cache = None
        self._drives_cache_at = 0.0
        self._meta_cache = {}  # file_id -> file dict

    async def aclose(self):
        await self._client.aclose()

    async def _auth_headers(self):
        tok = await self.tokens.get_token()
        return {"Authorization": "Bearer %s" % tok}

    async def _get(self, url, params):
        """GET with one 401-retry after a forced token refresh."""
        headers = await self._auth_headers()
        resp = await self._client.get(url, params=params, headers=headers)
        if resp.status_code == 401:
            await self.tokens.force_refresh()
            headers = await self._auth_headers()
            resp = await self._client.get(url, params=params, headers=headers)
        if resp.status_code >= 400:
            self._raise_for(resp)
        return resp.json()

    def _raise_for(self, resp):
        reason = None
        message = "Drive API error %s" % resp.status_code
        try:
            body = resp.json()
            err = body.get("error", {})
            message = err.get("message", message)
            errors = err.get("errors") or []
            if errors:
                reason = errors[0].get("reason")
        except Exception:
            pass
        raise DriveAPIError(resp.status_code, message, reason)

    # ---- drives ----

    async def list_drives(self, force=False):
        """Return [{"id","name"}, ...] of Shared Drives (cached ~5 min)."""
        now = time.time()
        if not force and self._drives_cache is not None and (now - self._drives_cache_at) < DRIVES_CACHE_TTL:
            return self._drives_cache
        raw = await self._drives_lister()
        drives = [
            {"id": d.get("id"), "name": d.get("name")}
            for d in raw
            if d.get("id")
        ]
        drives.sort(key=lambda d: (d.get("name") or "").lower())
        self._drives_cache = drives
        self._drives_cache_at = now
        return drives

    # ---- browse ----

    async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200):
        """List folders + video files within a folder of a Shared Drive.

        Root folder id == the drive id itself.
        """
        parent = folder_id or drive_id
        q = "'%s' in parents and trashed = false" % escape_q(parent)
        params = {
            "q": q,
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": LIST_FIELDS,
            "orderBy": "folder,name",
            "pageSize": str(page_size),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await self._get(FILES_URL, params)
        files = self._filter_browse(data.get("files", []))
        self._cache_metas(files)
        return {"files": files, "nextPageToken": data.get("nextPageToken")}

    def _filter_browse(self, files):
        """Keep folders and video/* files; drop other google-apps types."""
        out = []
        for f in files:
            mime = f.get("mimeType", "")
            if mime == FOLDER_MIME:
                out.append(f)
            elif mime.startswith("video/"):
                out.append(f)
            # everything else (docs, sheets, images, audio, etc.) is dropped
        return out

    # ---- search ----

    async def search(self, query, page_token=None, page_size=200):
        """Search video files across all drives by name substring."""
        term = escape_q(query)
        q = "name contains '%s' and mimeType contains 'video/' and trashed = false" % term
        params = {
            "q": q,
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": LIST_FIELDS,
            "orderBy": "name",
            "pageSize": str(page_size),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await self._get(FILES_URL, params)
        files = [f for f in data.get("files", []) if f.get("mimeType", "").startswith("video/")]
        self._cache_metas(files)
        return {"files": files, "nextPageToken": data.get("nextPageToken")}

    # ---- metadata ----

    def _cache_metas(self, files):
        for f in files:
            if f.get("id"):
                self._meta_cache[f["id"]] = f

    async def file_meta(self, file_id, force=False):
        """Return metadata for a single file (cached)."""
        if not force and file_id in self._meta_cache:
            return self._meta_cache[file_id]
        params = {
            "supportsAllDrives": "true",
            "fields": FILE_FIELDS,
        }
        data = await self._get("%s/%s" % (FILES_URL, file_id), params)
        self._meta_cache[file_id] = data
        return data
