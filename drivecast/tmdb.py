"""Optional TMDB enrichment: posters + metadata for parsed titles.

If no API key is configured this module is inert (enrich() returns None for
everything). Lookups — including negative results — are cached to
data/tmdb_cache.json, and posters are downloaded once to data/posters/.
"""
import asyncio
import json
import os
import threading

import httpx

from . import config

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w342"
CACHE_PATH = os.path.join(config.DATA_DIR, "tmdb_cache.json")


class TMDB:
    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()
        self.enabled = bool(self.api_key)
        self._cache = self._load_cache()
        self._cache_lock = threading.Lock()
        self._sem = asyncio.Semaphore(4)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0)) if self.enabled else None

    def _load_cache(self):
        try:
            with open(CACHE_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return self._heal_cache(data)
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def _heal_cache(data):
        """Drop positive entries cached before genre_ids existed so they get
        refetched (once) with genres. None negative markers stay valid."""
        return {k: v for k, v in data.items()
                if v is None or (isinstance(v, dict) and "genre_ids" in v)}

    def _save_cache(self):
        with self._cache_lock:
            try:
                os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._cache, f, indent=2)
                os.replace(tmp, CACHE_PATH)
            except OSError:
                pass

    async def aclose(self):
        if self._client:
            await self._client.aclose()

    def _cache_key(self, title, year, media_type):
        return "%s|%s|%s" % (media_type, (title or "").lower(), year or "")

    async def enrich(self, title, year=None, media_type="movie"):
        """Return {"tmdb_id","title","year","poster_key","overview"} or None.

        Negative results are cached as an explicit None marker.
        """
        if not self.enabled:
            return None
        key = self._cache_key(title, year, media_type)
        with self._cache_lock:
            hit = key in self._cache
            cached = self._cache.get(key)
        if hit:
            await self._ensure_poster(cached)
            return cached

        async with self._sem:
            # Double-check cache inside the semaphore.
            with self._cache_lock:
                hit = key in self._cache
                cached = self._cache.get(key)
            if hit:
                await self._ensure_poster(cached)
                return cached
            result = await self._lookup(title, year, media_type)

        with self._cache_lock:
            self._cache[key] = result
        self._save_cache()
        return result

    async def _lookup(self, title, year, media_type):
        endpoint = "/search/tv" if media_type == "tv" else "/search/movie"
        params = {"api_key": self.api_key, "query": title, "include_adult": "false"}
        if year:
            params["year" if media_type == "movie" else "first_air_date_year"] = str(year)
        try:
            resp = await self._client.get(TMDB_BASE + endpoint, params=params)
            if resp.status_code != 200:
                return None
            results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return None
        if not results:
            return None
        top = results[0]
        poster_path = top.get("poster_path")
        poster_key = None
        if poster_path:
            poster_key = poster_path.lstrip("/")
            await self._download_poster(poster_path, poster_key)
        return {
            "tmdb_id": top.get("id"),
            "title": top.get("title") or top.get("name") or title,
            "year": (top.get("release_date") or top.get("first_air_date") or "")[:4] or year,
            "poster_key": poster_key,
            "overview": top.get("overview") or None,
            "genre_ids": top.get("genre_ids") or [],
        }

    async def _ensure_poster(self, result):
        """Guarantee a cached hit's poster image is actually on disk.

        enrich() returns cached lookups without re-querying TMDB, so it must
        also re-materialise the poster: a file can go missing after the lookup
        was cached (the title was reclassified and its old poster pruned, or the
        original download failed after the positive result was cached). Without
        this, such a title shows a permanent placeholder because nothing ever
        re-downloads its poster. _download_poster is a no-op when the file
        already exists, so this costs one stat on the common path.
        """
        pk = result.get("poster_key") if isinstance(result, dict) else None
        if pk:
            await self._download_poster("/" + pk, pk)

    async def _download_poster(self, poster_path, poster_key):
        dest = os.path.join(config.POSTERS_DIR, poster_key)
        if os.path.exists(dest):
            return
        try:
            resp = await self._client.get(IMG_BASE + poster_path)
            if resp.status_code == 200:
                os.makedirs(config.POSTERS_DIR, exist_ok=True)
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(resp.content)
                os.replace(tmp, dest)
        except (httpx.HTTPError, OSError):
            pass

    def poster_path(self, poster_key):
        """Local filesystem path for a cached poster, or None if absent."""
        if not poster_key:
            return None
        p = os.path.join(config.POSTERS_DIR, poster_key)
        return p if os.path.exists(p) else None
