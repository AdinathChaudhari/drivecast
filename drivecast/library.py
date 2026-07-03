"""Cached media library: scan Shared Drives into movie/show records.

The library is the primary browsing surface. A scan walks each SELECTED drive's
folder tree once (quota-resilient, throttled) and produces a structured,
persisted catalogue at data/library.json, so normal browsing hits the Drive API
zero times — tiles, seasons, episodes and pre-cached posters all come off disk.

Design split:
  * Pure functions (classify_node, classify_loose, diff_library, ...) operate on
    plain dicts and are exhaustively unit-tested on synthetic data — no network.
  * The Scanner does the I/O: it walks drives via DriveAPI (which handles
    rate-limit backoff), resolves posters via TMDB, diffs against the existing
    library, prunes orphaned posters, and writes atomically.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time

from . import config
from . import naming
from .drive_api import FOLDER_MIME, DriveAPIError

log = logging.getLogger("drivecast.library")

LIBRARY_PATH = os.path.join(config.DATA_DIR, "library.json")
LIBRARY_VERSION = 1

# Guess a mimeType from a filename so seeded HEAD responses are sensible.
_EXT_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
}


def _mime_for(name):
    _, ext = os.path.splitext(name or "")
    return _EXT_MIME.get(ext.lower(), "video/mp4")


def _is_folder(f):
    return f.get("mimeType") == FOLDER_MIME


def _is_video(f):
    return (f.get("mimeType") or "").startswith("video/")


def _duration_ms(f):
    vmm = f.get("videoMediaMetadata") or {}
    d = vmm.get("durationMillis")
    try:
        return int(d) if d is not None else None
    except (TypeError, ValueError):
        return None


def _video_dict(f, parent_id, ancestors):
    """Normalise a raw Drive file into the internal video shape used by classify."""
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": f.get("id"),
        "name": f.get("name") or "",
        "size": size,
        "duration_ms": _duration_ms(f),
        "parent_id": parent_id,
        "ancestors": list(ancestors),
    }


def _loose_show_id(drive_id, title):
    h = hashlib.sha1(("%s|%s" % (drive_id, title.lower())).encode("utf-8")).hexdigest()
    return "loose:" + h[:16]


# ------------------------------------------------------------------ classify --

def _season_of(video):
    """Determine a video's season: nearest season-named ancestor, else SxxExx, else 1."""
    for anc in reversed(video["ancestors"]):
        s = naming.season_from_folder(anc)
        if s is not None:
            return s
    ep = naming.detect_episode(video["name"])
    if ep is not None:
        return ep[0]
    return 1


def _episode_record(video, show_title, fallback_number):
    e = naming.episode_number(video["name"])
    title = naming.episode_title(video["name"])
    if not title:
        title = "Episode %d" % e if e is not None else naming.strip_ext(video["name"])
    return {
        "title": title,
        "episode": e if e is not None else fallback_number,
        "file_id": video["id"],
        "name": video["name"],
        "duration_ms": video["duration_ms"],
        "size": video["size"],
        "parent_id": video["parent_id"],
    }


def _group_episodes(videos, show_title):
    """Group videos into ascending seasons, each with episodes sorted by number."""
    buckets = {}
    for v in videos:
        buckets.setdefault(_season_of(v), []).append(v)
    seasons = []
    for s in sorted(buckets):
        vids = buckets[s]
        vids.sort(key=lambda v: (
            naming.episode_number(v["name"]) if naming.episode_number(v["name"]) is not None else 10 ** 6,
            v["name"].lower(),
        ))
        episodes = [_episode_record(v, show_title, i + 1) for i, v in enumerate(vids)]
        seasons.append({"season": s, "episodes": episodes})
    return seasons


def _all_videos(node):
    """Flatten every descendant video under a node (recursively)."""
    out = list(node["videos"])
    for sf in node["subfolders"]:
        out.extend(_all_videos(sf))
    return out


def _is_show(node):
    """True if a folder node is a TV show.

    A show has a DIRECT season-named subfolder, OR >=2 of its videos (direct, or
    in its immediate season subfolders) carry episode markers. Detection looks at
    immediate children only — deeper folders are recursed into as their own tiles.
    """
    if any(naming.season_from_folder(sf["name"]) is not None for sf in node["subfolders"]):
        return True
    count = sum(1 for v in node["videos"] if naming.episode_number(v["name"]) is not None)
    for sf in node["subfolders"]:
        if naming.season_from_folder(sf["name"]) is not None:
            count += sum(1 for v in sf["videos"] if naming.episode_number(v["name"]) is not None)
    return count >= 2


def _show_record(node):
    """Build one show record from a node's descendant videos, or None if empty."""
    videos = [v for v in _all_videos(node) if not naming.is_sample(v["name"])]
    if not videos:
        return None
    title, year = naming.clean_title(node["name"])
    return {
        "id": node["id"],
        "type": "show",
        "title": title,
        "year": year,
        "drive_id": node["drive_id"],
        "folder_id": node["id"],
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        # Raw folder name kept transiently so the season-grouping pass can detect
        # "<Show> Season N" / bare "Season N" siblings. Stripped before persisting.
        "_folder_name": node["name"],
        "seasons": _group_episodes(videos, title),
    }


def _movie_record(node, video, from_folder):
    """One movie record for a single video; title from the folder or the file."""
    title, year = naming.clean_title(node["name"] if from_folder else video["name"])
    return {
        "id": video["id"],
        "type": "movie",
        "title": title,
        "year": year,
        "drive_id": node["drive_id"],
        "folder_id": node["id"],
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "file_id": video["id"],
        "size": video["size"],
        "duration_ms": video["duration_ms"],
        # Transient keys kept so group_seasons can still merge a stray movie folder
        # that turns out to be a bare/prefixed season; stripped before persisting.
        "_folder_name": node["name"],
        "_video_name": video["name"],
    }


def _expand_movies(node):
    """Expand a non-show node into movie records, recursing into subfolders.

    * leaf movie folder (videos, no non-extras subfolders): 1 video -> one movie
      named from the FOLDER; multiple videos -> one movie per video (file-named).
    * container (has non-extras subfolders): each stray direct video becomes a
      file-named movie, plus the recursion into each non-extras subfolder.
    Extras subfolders (Featurettes/Extras/...) are ignored entirely.
    """
    direct = [v for v in node["videos"] if not naming.is_sample(v["name"])]
    subs = [sf for sf in node["subfolders"] if not naming.is_extras_folder(sf["name"])]

    if not subs:
        if not direct:
            return []
        if len(direct) == 1:
            return [_movie_record(node, direct[0], from_folder=True)]
        return [_movie_record(node, v, from_folder=False) for v in direct]

    records = [_movie_record(node, v, from_folder=False) for v in direct]
    for sf in subs:
        records.extend(classify_node(sf))
    return records


def classify_node(node):
    """Classify one folder node into a list of movie/show records (recursive).

    node = {
      "id", "name", "drive_id",
      "videos":     [ _video_dict(...) ],  # videos DIRECTLY in this folder
      "subfolders": [ child node, ... ],   # nested child nodes (same shape)
    }

    A show yields a single show record; anything else expands into one or more
    movie tiles, recursing through collection/container folders so each film is
    its own tile.
    """
    if _is_show(node):
        rec = _show_record(node)
        return [rec] if rec else []
    return _expand_movies(node)


def classify_loose(drive_id, loose_files):
    """Classify loose video files at a drive root into shows / standalone movies.

    Files with SxxExx/NxNN markers group into shows keyed by parsed show title;
    everything else is a standalone movie.
    """
    videos = [f for f in loose_files if _is_video(f) and not naming.is_sample(f.get("name") or "")]
    shows = {}
    movies = []
    for f in videos:
        if naming.detect_episode(f.get("name") or "") is not None:
            st = naming.parse(f["name"])["title"]
            shows.setdefault(st, []).append(f)
        else:
            movies.append(f)

    records = []
    for show_title, files in shows.items():
        vids = [_video_dict(f, drive_id, []) for f in files]
        year = None
        for f in files:
            year = naming.parse(f["name"])["year"]
            if year:
                break
        records.append({
            "id": _loose_show_id(drive_id, show_title),
            "type": "show",
            "title": show_title,
            "year": year,
            "drive_id": drive_id,
            "folder_id": drive_id,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "seasons": _group_episodes(vids, show_title),
        })
    for f in movies:
        title, year = naming.clean_title(f["name"])
        v = _video_dict(f, drive_id, [])
        records.append({
            "id": f.get("id"),
            "type": "movie",
            "title": title,
            "year": year,
            "drive_id": drive_id,
            "folder_id": None,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "file_id": v["id"],
            "size": v["size"],
            "duration_ms": v["duration_ms"],
        })
    return records


# ------------------------------------------------------------- season grouping --

# Drive-name noise to strip when a whole drive is one show (e.g. "TV | Blackadder",
# "Movie // MCU", "Malcolm in the Middle (Part 1)").
_DRIVE_PREFIX_RE = re.compile(r"^\s*(?:tv|movie|movies|show|shows)\s*[|/:\-]+\s*", re.IGNORECASE)
_PART_SUFFIX_RE = re.compile(r"\s*[\(\[]?\s*part\s*\d+\s*[\)\]]?\s*$", re.IGNORECASE)


def _clean_show_name(name):
    """Human display name for a show derived from a drive/prefix name."""
    n = _DRIVE_PREFIX_RE.sub("", name or "")
    n = _PART_SUFFIX_RE.sub("", n)
    return n.strip(" -_|/").strip() or (name or "").strip()


def _norm_key(name):
    """Normalised grouping key: lowercased, de-noised, alphanumerics only."""
    n = _clean_show_name(name).lower()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _episodes_of(rec, fallback_season):
    """Flatten a member record's videos into episode dicts (movie -> 1 episode)."""
    if rec.get("type") == "show":
        eps = []
        for s in rec.get("seasons", []):
            eps.extend(s.get("episodes", []))
        return eps
    return [{
        "title": rec.get("title"),
        "episode": 1,
        "file_id": rec.get("file_id"),
        "name": rec.get("_video_name") or rec.get("title"),
        "duration_ms": rec.get("duration_ms"),
        "size": rec.get("size"),
        "parent_id": rec.get("folder_id"),
    }]


def group_seasons(records, drive_names):
    """Merge sibling season-folders (and single-show drives) into one show each.

    Handles the two layouts that otherwise show one tile per season:
      * bare "Season N" top-level folders  -> the DRIVE is the show (name = drive)
      * "<Show> Season N" sibling folders  -> grouped by the shared show prefix
    Nested "Show/Season N" folders already classify correctly and pass through.
    Records sharing a normalised key merge across drives too (e.g. a show split
    into "... (Part 1)" / "(Part 2)" drives).
    """
    groups = {}          # key -> {"display","drive_id","year","members":[(season,rec)]}
    order = []           # preserve first-seen ordering for stable output
    passthrough = []

    for rec in records:
        fname = rec.get("_folder_name")
        drive_id = rec.get("drive_id")
        drive_name = drive_names.get(drive_id, "") if drive_id else ""

        key = display = None
        season_num = None
        if fname is not None:
            ps = naming.pure_season(fname)
            if ps is not None:
                key = _norm_key(drive_name) or ("drive:" + str(drive_id))
                display = _clean_show_name(drive_name) or "Unknown"
                season_num = ps
            else:
                prefix, snum = naming.split_season_suffix(fname)
                if prefix:
                    key = _norm_key(prefix)
                    display = _clean_show_name(prefix)
                    season_num = snum

        if key is None:
            passthrough.append(rec)
            continue

        g = groups.get(key)
        if g is None:
            g = {"display": display, "drive_id": drive_id, "year": rec.get("year"),
                 "members": []}
            groups[key] = g
            order.append(key)
        if not g["year"] and rec.get("year"):
            g["year"] = rec.get("year")
        g["members"].append((season_num, rec))

    merged = []
    for key in order:
        g = groups[key]
        buckets = {}
        for season_num, rec in g["members"]:
            buckets.setdefault(season_num, []).extend(_episodes_of(rec, season_num))
        seasons = []
        for s in sorted(buckets):
            eps = buckets[s]
            eps.sort(key=lambda e: (e.get("episode") if e.get("episode") is not None else 10 ** 6,
                                    (e.get("name") or "").lower()))
            seasons.append({"season": s, "episodes": eps})
        merged.append({
            "id": "grp:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
            "type": "show",
            "title": g["display"],
            "year": g["year"],
            "drive_id": g["drive_id"],
            "folder_id": None,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "seasons": seasons,
        })

    return _strip_transient(passthrough) + merged


def _strip_transient(records):
    """Remove the transient _folder_name/_video_name keys before persisting."""
    for rec in records:
        rec.pop("_folder_name", None)
        rec.pop("_video_name", None)
    return records


# ----------------------------------------------------------------------- diff --

def diff_library(old_titles, new_titles):
    """Return (added_ids, removed_ids) comparing title id sets."""
    old_ids = set(old_titles)
    new_ids = set(new_titles)
    added = [tid for tid in new_titles if tid not in old_ids]
    removed = [tid for tid in old_titles if tid not in new_ids]
    return added, removed


def merge_existing_metadata(old_titles, new_titles):
    """Carry poster/tmdb/overview from prior records into freshly-scanned ones.

    Avoids re-hitting TMDB for titles we already resolved; episode lists in the
    new record are kept as-scanned (that's the point of a refresh).
    """
    for tid, rec in new_titles.items():
        old = old_titles.get(tid)
        if not old:
            continue
        for k in ("poster", "tmdb_id", "overview"):
            if old.get(k) and not rec.get(k):
                rec[k] = old[k]


def prune_removed_posters(old_titles, new_titles, removed_ids):
    """Delete poster files belonging to removed titles that nothing else uses."""
    keep = {t.get("poster") for t in new_titles.values() if t.get("poster")}
    for tid in removed_ids:
        pk = old_titles.get(tid, {}).get("poster")
        if pk and pk not in keep:
            try:
                os.remove(os.path.join(config.POSTERS_DIR, pk))
            except OSError:
                pass


# -------------------------------------------------------------------- Library --

class Library:
    """In-memory catalogue backed by data/library.json (atomic writes)."""

    def __init__(self, path=LIBRARY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load()
        self._file_index = {}
        self._rebuild_index()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("titles"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": LIBRARY_VERSION, "generated_at": 0, "titles": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".library-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _rebuild_index(self):
        """Map file_id -> {name,size,duration_ms} for fast play/HEAD lookups."""
        idx = {}
        for rec in self.data["titles"].values():
            if rec.get("type") == "movie" and rec.get("file_id"):
                idx[rec["file_id"]] = {
                    "name": rec.get("title"),
                    "size": rec.get("size"),
                    "duration_ms": rec.get("duration_ms"),
                }
            elif rec.get("type") == "show":
                for season in rec.get("seasons", []):
                    for ep in season.get("episodes", []):
                        if ep.get("file_id"):
                            idx[ep["file_id"]] = {
                                "name": ep.get("name"),
                                "size": ep.get("size"),
                                "duration_ms": ep.get("duration_ms"),
                            }
        self._file_index = idx

    # ---- reads ----

    def snapshot_titles(self):
        with self._lock:
            return json.loads(json.dumps(self.data["titles"]))

    def titles_list(self):
        with self._lock:
            return list(self.data["titles"].values())

    def get(self, title_id):
        with self._lock:
            return self.data["titles"].get(title_id)

    def generated_at(self):
        return self.data.get("generated_at", 0)

    def is_empty(self):
        return not self.data["titles"]

    def file_info(self, file_id):
        return self._file_index.get(file_id)

    # ---- writes ----

    def replace(self, titles):
        with self._lock:
            self.data = {
                "version": LIBRARY_VERSION,
                "generated_at": time.time(),
                "titles": titles,
            }
            self._rebuild_index()
            self._save()

    def seed_api_cache(self, api):
        """Prime the DriveAPI metadata cache so playback needs no Drive call."""
        with self._lock:
            titles = list(self.data["titles"].values())
        for rec in titles:
            if rec.get("type") == "movie" and rec.get("file_id"):
                api.seed_meta(self._meta(rec["file_id"], rec.get("title"),
                                         rec.get("size"), rec.get("duration_ms")))
            elif rec.get("type") == "show":
                for season in rec.get("seasons", []):
                    for ep in season.get("episodes", []):
                        if ep.get("file_id"):
                            api.seed_meta(self._meta(ep["file_id"], ep.get("name"),
                                                     ep.get("size"), ep.get("duration_ms")))

    @staticmethod
    def _meta(file_id, name, size, duration_ms):
        meta = {
            "id": file_id,
            "name": name,
            "mimeType": _mime_for(name),
        }
        if size:
            meta["size"] = str(size)
        if duration_ms:
            meta["videoMediaMetadata"] = {"durationMillis": str(duration_ms)}
        return meta


# -------------------------------------------------------------------- Scanner --

class Scanner:
    """Walks selected drives and (re)builds the library, quota-resiliently."""

    def __init__(self, api, tmdb, library, throttle=0.15):
        self.api = api
        self.tmdb = tmdb
        self.library = library
        self.throttle = throttle
        self.status = {
            "running": False, "scanned": 0, "total": 0,
            "added": 0, "removed": 0, "error": None,
        }

    async def _throttle(self):
        if self.throttle:
            await asyncio.sleep(self.throttle)

    async def _list_folder(self, drive_id, folder_id):
        """Return all raw children of a folder, following pagination + throttle."""
        out = []
        page_token = None
        while True:
            res = await self.api.browse(drive_id, folder_id, page_token)
            out.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            await self._throttle()
            if not page_token:
                break
        return out

    async def _walk_title(self, drive_id, folder, ancestors=()):
        """Recursively build a nested tree node preserving folder hierarchy.

        node = {id, name, drive_id, videos:[direct videos], subfolders:[child nodes]}
        Each video carries its ancestor folder-name chain so season detection and
        episode grouping still work when a show is nested inside a container.
        """
        name = folder.get("name") or ""
        node = {
            "id": folder.get("id"),
            "name": name,
            "drive_id": drive_id,
            "videos": [],
            "subfolders": [],
        }
        child_anc = tuple(ancestors) + (name,)
        children = await self._list_folder(drive_id, node["id"])
        for c in children:
            if _is_folder(c):
                node["subfolders"].append(await self._walk_title(drive_id, c, child_anc))
            elif _is_video(c):
                node["videos"].append(_video_dict(c, node["id"], child_anc))
        return node

    async def _scan_drive(self, drive_id):
        """Return the list of title records for one drive (never raises)."""
        try:
            root = await self._list_folder(drive_id, drive_id)
        except DriveAPIError as e:
            log.warning("Skipping drive %s: %s", drive_id, e.message)
            self.status["error"] = "Drive %s: %s" % (drive_id, e.message)
            return []

        records = []
        loose = []
        for entry in root:
            if _is_folder(entry):
                try:
                    node = await self._walk_title(drive_id, entry)
                except DriveAPIError as e:
                    log.warning("Skipping folder %r: %s", entry.get("name"), e.message)
                    self.status["error"] = "Folder %s: %s" % (entry.get("name"), e.message)
                    continue
                records.extend(classify_node(node))
            elif _is_video(entry):
                loose.append(entry)
        records.extend(classify_loose(drive_id, loose))
        return records

    async def _resolve_poster(self, rec):
        if not self.tmdb.enabled:
            return
        media = "tv" if rec["type"] == "show" else "movie"
        try:
            meta = await self.tmdb.enrich(rec["title"], rec.get("year"), media)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("TMDB lookup failed for %r: %r", rec["title"], e)
            return
        if not meta:
            return
        rec["poster"] = meta.get("poster_key")
        rec["tmdb_id"] = meta.get("tmdb_id")
        rec["overview"] = meta.get("overview")
        if meta.get("year") and not rec.get("year"):
            try:
                rec["year"] = int(str(meta["year"])[:4])
            except (TypeError, ValueError):
                pass

    async def scan(self, selected_drives):
        """Full scan + diff + poster refresh + persist. Never raises."""
        self.status.update(running=True, scanned=0, total=len(selected_drives),
                           added=0, removed=0, error=None)
        try:
            all_records = []
            for drive_id in selected_drives:
                all_records.extend(await self._scan_drive(drive_id))
                self.status["scanned"] += 1

            # Merge sibling/bare season folders into single shows (needs drive names).
            try:
                drive_names = {d["id"]: d.get("name", "") for d in await self.api.list_drives()}
            except Exception:
                drive_names = {}
            grouped = group_seasons(all_records, drive_names)
            new_titles = {rec["id"]: rec for rec in grouped}

            old_titles = self.library.snapshot_titles()
            added, removed = diff_library(old_titles, new_titles)
            merge_existing_metadata(old_titles, new_titles)

            self.status["added"] = len(added)

            # Resolve posters for EVERY title still missing one when TMDB is
            # enabled. This backfills existing titles the first time a key is
            # configured; titles that already have a poster are skipped.
            if self.tmdb.enabled:
                for rec in new_titles.values():
                    if not rec.get("poster"):
                        await self._resolve_poster(rec)

            prune_removed_posters(old_titles, new_titles, removed)
            self.status["removed"] = len(removed)

            self.library.replace(new_titles)
            self.library.seed_api_cache(self.api)
        except Exception as e:  # pragma: no cover - defensive, keep the app alive
            log.exception("Library scan failed")
            self.status["error"] = str(e)
        finally:
            self.status["running"] = False
        return self.status
