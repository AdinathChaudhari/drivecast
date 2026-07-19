"""TMDB enrichment tests.

Config paths (incl. POSTERS_DIR) are sandboxed to a temp dir by the autouse
conftest fixture, so these never touch real cached posters or hit the network
(the only method that would make a request, _download_poster, is stubbed)."""
import asyncio

from drivecast import tmdb as tmdb_mod


def test_enrich_cache_hit_redownloads_missing_poster(monkeypatch):
    """A cached positive lookup whose poster file is gone (pruned after a
    reclassify, or a download that failed after the result was cached) must be
    re-materialised on the next enrich() — otherwise the title shows a
    permanent placeholder because nothing re-fetches its poster."""
    t = tmdb_mod.TMDB("fake-key")
    calls = []

    async def fake_download(poster_path, poster_key):
        calls.append((poster_path, poster_key))

    monkeypatch.setattr(t, "_download_poster", fake_download)
    key = t._cache_key("Iron Man", 2008, "movie")
    t._cache[key] = {"tmdb_id": 1726, "title": "Iron Man", "year": "2008",
                     "poster_key": "abc.jpg", "overview": None, "genre_ids": []}

    res = asyncio.run(t.enrich("Iron Man", 2008, "movie"))
    assert res["poster_key"] == "abc.jpg"          # cached result returned
    assert calls == [("/abc.jpg", "abc.jpg")]      # and its poster re-fetched
    asyncio.run(t.aclose())


def test_enrich_cache_hit_negative_marker_does_not_download(monkeypatch):
    """A cached NEGATIVE result (None marker) is returned as-is with no poster
    work — _ensure_poster must tolerate a None cache entry."""
    t = tmdb_mod.TMDB("fake-key")
    calls = []

    async def fake_download(poster_path, poster_key):
        calls.append((poster_path, poster_key))

    monkeypatch.setattr(t, "_download_poster", fake_download)
    key = t._cache_key("Nonexistent Film", None, "movie")
    t._cache[key] = None

    res = asyncio.run(t.enrich("Nonexistent Film", None, "movie"))
    assert res is None
    assert calls == []
    asyncio.run(t.aclose())
