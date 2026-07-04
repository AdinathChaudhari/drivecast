"""Tests for the library: classification, grouping, diff, backoff, scan.

All synthetic — no Drive API is ever contacted.
"""
import asyncio
import os

import pytest

from drivecast import config, library
from drivecast.drive_api import FOLDER_MIME, DriveAPI, DriveAPIError


# ------------------------------------------------------------- helpers --------

def vid(fid, name, size=1000, dur=None, parent="p", ancestors=()):
    return {"id": fid, "name": name, "size": size, "duration_ms": dur,
            "parent_id": parent, "ancestors": list(ancestors)}


def node(name, videos, subfolders=(), fid="folder1", drive="drv1"):
    """Build a tree node. `subfolders` is a list of child NODES (nested)."""
    return {"id": fid, "name": name, "drive_id": drive,
            "videos": list(videos), "subfolders": list(subfolders)}


def rawfile(fid, name, mime="video/mp4", size=1000, dur=None, thumb=None):
    f = {"id": fid, "name": name, "mimeType": mime, "size": str(size)}
    if dur is not None:
        f["videoMediaMetadata"] = {"durationMillis": str(dur)}
    if thumb is not None:
        f["thumbnailLink"] = thumb
    return f


def rawfolder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


# ------------------------------------------------------- classify: movie ------

def only(recs):
    """Assert a single record was produced and return it."""
    assert len(recs) == 1, recs
    return recs[0]


def test_classify_single_movie_folder_titled_from_folder():
    n = node("Your Name (2016) [BluRay] [1080p]",
             [vid("v1", "Your.Name.2016.1080p.BluRay.mkv", size=5000, dur=360000)])
    rec = only(library.classify_node(n))
    assert rec["type"] == "movie"
    assert rec["title"] == "Your Name"   # from the FOLDER name
    assert rec["year"] == 2016
    assert rec["id"] == "v1"             # id == the video's file_id
    assert rec["file_id"] == "v1"
    assert rec["folder_id"] == "folder1"
    assert rec["duration_ms"] == 360000
    assert rec["size"] == 5000


def test_leaf_folder_multiple_videos_expands_per_file():
    # A leaf folder with >1 main video -> one movie per file (file-named),
    # and sample/trailer files are ignored.
    n = node("Double Feature", [
        vid("s", "Movie-sample.mkv", size=9999),        # sample: excluded
        vid("t", "Movie-trailer.mp4", size=8888),       # trailer: excluded
        vid("a", "Mad Max Fury Road 2015 1080p.mkv", size=5000),
        vid("b", "Sicario 2015 1080p.mkv", size=1000),
    ])
    recs = library.classify_node(n)
    by_id = {r["file_id"]: r for r in recs}
    assert set(by_id) == {"a", "b"}          # only the two real videos
    assert all(r["type"] == "movie" for r in recs)
    assert by_id["a"]["title"] == "Mad Max Fury Road"   # from the FILE name
    assert by_id["b"]["title"] == "Sicario"


def test_empty_folder_returns_no_records():
    assert library.classify_node(node("Empty", [])) == []
    # Folder with only a sample is effectively empty.
    assert library.classify_node(node("OnlySample", [vid("s", "movie-sample.mkv")])) == []


# ------------------------------------------------ classify: recursion ---------

def test_container_of_movie_folders_expands_to_one_tile_each():
    # A collection folder ("Phase 1") of enumerated single-movie subfolders must
    # become one tile per movie, NOT a single "Phase 1" tile.
    iron = node("01) Iron Man (2008) [1080p]",
                [vid("f1", "01.Iron Man (2008) 1080p.mkv")], fid="ironF")
    hulk = node("02) The Incredible Hulk (2008) [1080p]",
                [vid("f2", "02.The Incredible Hulk (2008) 1080p.mkv")], fid="hulkF")
    phase = node("Phase 1", [], subfolders=[iron, hulk], fid="phaseF")
    recs = library.classify_node(phase)
    titles = {r["title"] for r in recs}
    assert titles == {"Iron Man", "The Incredible Hulk"}
    assert "Phase 1" not in titles
    assert {r["file_id"] for r in recs} == {"f1", "f2"}


def test_extras_subfolder_is_ignored():
    # A movie folder with a Featurettes extras subfolder is still one movie.
    extras = node("Featurettes", [vid("x", "Behind the scenes.mkv")], fid="exF")
    movie = node("Blade - Trinity (2004) [1080p]",
                 [vid("m", "Blade.Trinity.2004.1080p.mkv")],
                 subfolders=[extras], fid="bladeF")
    rec = only(library.classify_node(movie))
    assert rec["type"] == "movie"
    assert rec["title"] == "Blade - Trinity"
    assert rec["file_id"] == "m"


def test_container_with_stray_videos_and_subfolders():
    # "Hollywood": loose movie files alongside a movie subfolder -> each is a tile.
    sub = node("The Godfather (1972)",
               [vid("g", "The.Godfather.1972.1080p.mkv")], fid="gfF")
    hollywood = node("Hollywood", [
        vid("h1", "Whiplash 2014 1080p.mkv"),
        vid("h2", "Sicario 2015 1080p.mkv"),
    ], subfolders=[sub], fid="hwF")
    recs = library.classify_node(hollywood)
    titles = {r["title"] for r in recs}
    assert titles == {"Whiplash", "Sicario", "The Godfather"}


def test_nested_containers_recurse_to_movies():
    # Container of a container of movie-folders.
    inner_movie = node("Blade (1998) [1080p]",
                       [vid("bl", "Blade.1998.1080p.mkv")], fid="blF")
    blade_series = node("Blade Series", [], subfolders=[inner_movie], fid="bsF")
    top = node("Hollywood", [], subfolders=[blade_series], fid="topF")
    rec = only(library.classify_node(top))
    assert rec["title"] == "Blade"
    assert rec["file_id"] == "bl"


# ------------------------------------------------------- classify: show -------

def test_show_by_season_subfolders():
    s1 = node("Season 1", [
        vid("e1", "Ep 01.mkv", parent="s1", ancestors=["The Bear", "Season 1"]),
        vid("e2", "Ep 02.mkv", parent="s1", ancestors=["The Bear", "Season 1"]),
    ], fid="s1")
    s2 = node("Season 2", [
        vid("e3", "Ep 01.mkv", parent="s2", ancestors=["The Bear", "Season 2"]),
    ], fid="s2")
    rec = only(library.classify_node(node("The Bear", [], subfolders=[s1, s2])))
    assert rec["type"] == "show"
    seasons = {s["season"]: s for s in rec["seasons"]}
    assert set(seasons) == {1, 2}
    assert len(seasons[1]["episodes"]) == 2
    assert [s["season"] for s in rec["seasons"]] == [1, 2]  # ascending


def test_show_by_flat_sxxexx_episodes():
    videos = [
        vid("e2", "Breaking.Bad.S05E02.mkv"),
        vid("e1", "Breaking.Bad.S05E01.mkv"),
        vid("e3", "Breaking.Bad.S05E10.mkv"),
    ]
    rec = only(library.classify_node(node("Breaking Bad", videos)))
    assert rec["type"] == "show"
    eps = rec["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2, 10]  # sorted by episode number
    assert rec["seasons"][0]["season"] == 5


def test_single_episode_is_still_a_movie_heuristic():
    # One episode-named file and no season folder -> falls through to movie.
    rec = only(library.classify_node(node("Random", [vid("v", "Random.S01E01.mkv")])))
    assert rec["type"] == "movie"


def test_episode_title_extracted():
    videos = [
        vid("a", "Frasier (1993) - S05E10 - Where Every Bloke.mkv"),
        vid("b", "Frasier (1993) - S05E11 - Perspectives.mkv"),
    ]
    rec = only(library.classify_node(node("Frasier", videos)))
    eps = rec["seasons"][0]["episodes"]
    assert eps[0]["title"] == "Where Every Bloke"


# ------------------------------------------------------- classify: loose ------

def test_loose_sxxexx_files_group_into_show():
    loose = [
        rawfile("e1", "The Office S03E01 720p.mkv"),
        rawfile("e2", "The Office S03E02 720p.mkv"),
        rawfile("m1", "Whiplash 2014 1080p.mkv"),
    ]
    recs = library.classify_loose("drv1", loose)
    shows = [r for r in recs if r["type"] == "show"]
    movies = [r for r in recs if r["type"] == "movie"]
    assert len(shows) == 1
    assert shows[0]["title"] == "The Office"
    assert shows[0]["id"].startswith("loose:")
    assert len(shows[0]["seasons"][0]["episodes"]) == 2
    assert len(movies) == 1 and movies[0]["title"] == "Whiplash"
    assert movies[0]["id"] == "m1"


def test_loose_sample_excluded():
    recs = library.classify_loose("drv1", [rawfile("s", "Movie-sample.mkv")])
    assert recs == []


# ------------------------------------------------------------- diff -----------

def test_diff_add_remove():
    old = {"a": {"id": "a"}, "b": {"id": "b"}}
    new = {"b": {"id": "b"}, "c": {"id": "c"}}
    added, removed = library.diff_library(old, new)
    assert added == ["c"]
    assert removed == ["a"]


def test_movie_record_has_quality_from_filename():
    n = node("Some Folder", [vid("v1", "Arrival.2016.2160p.UHD.BluRay.mkv")])
    rec = only(library.classify_node(n))
    assert rec["quality"] == "4K"


def test_movie_record_quality_falls_back_to_folder_name():
    # No quality in the file name, but the folder carries it.
    n = node("Arrival (2016) 1080p BluRay", [vid("v1", "Arrival.mkv")])
    rec = only(library.classify_node(n))
    assert rec["quality"] == "1080p"


def test_show_record_uses_best_episode_quality():
    s1 = node("Season 1", [
        vid("e1", "Show.S01E01.720p.mkv", ancestors=["Show", "Season 1"]),
        vid("e2", "Show.S01E02.1080p.mkv", ancestors=["Show", "Season 1"]),
        vid("e3", "Show.S01E03.480p.mkv", ancestors=["Show", "Season 1"]),
    ], fid="s1")
    rec = only(library.classify_node(node("Show", [], subfolders=[s1])))
    assert rec["type"] == "show"
    assert rec["quality"] == "1080p"   # best of 720p/1080p/480p


def test_loose_records_carry_quality():
    loose = [
        rawfile("e1", "The Office S03E01 2160p.mkv"),
        rawfile("e2", "The Office S03E02 720p.mkv"),
        rawfile("m1", "Whiplash 2014 1080p.mkv"),
    ]
    recs = library.classify_loose("drv1", loose)
    show = next(r for r in recs if r["type"] == "show")
    movie = next(r for r in recs if r["type"] == "movie")
    assert show["quality"] == "4K"      # best across the two episodes
    assert movie["quality"] == "1080p"


def test_grouped_show_carries_best_member_quality():
    m1 = _showrec("b1", "dBL", "Blackadder Season 1 S01", 1, 2)
    m1["quality"] = "SD"
    m2 = _showrec("b2", "dBL", "Blackadder Season 2 S02", 2, 2)
    m2["quality"] = "1080p"
    out = library.group_seasons([m1, m2], {"dBL": "TV | Blackadder"})
    assert len(out) == 1
    assert out[0]["quality"] == "1080p"


def test_assign_added_at_sets_and_preserves():
    old = {"a": {"id": "a", "added_at": 100.0}}
    new = {"a": {"id": "a"}, "b": {"id": "b"}}
    library.assign_added_at(old, new, now=555.0)
    assert new["a"]["added_at"] == 100.0   # preserved for existing title
    assert new["b"]["added_at"] == 555.0   # stamped for newly-added title


def test_merge_existing_metadata_carries_poster():
    old = {"a": {"id": "a", "poster": "p.jpg", "tmdb_id": 5, "overview": "o"}}
    new = {"a": {"id": "a", "poster": None, "tmdb_id": None, "overview": None}}
    library.merge_existing_metadata(old, new)
    assert new["a"]["poster"] == "p.jpg"
    assert new["a"]["tmdb_id"] == 5


def test_prune_removed_posters(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path))
    p1 = tmp_path / "gone.jpg"
    p2 = tmp_path / "shared.jpg"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    old = {"a": {"poster": "gone.jpg"}, "b": {"poster": "shared.jpg"}}
    new = {"c": {"poster": "shared.jpg"}}  # still references shared.jpg
    library.prune_removed_posters(old, new, ["a", "b"])
    assert not p1.exists()   # orphaned -> deleted
    assert p2.exists()       # still referenced -> kept


# ------------------------------------------------------- backoff --------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, url, params=None, headers=None):
        r = self.responses[self.calls]
        self.calls += 1
        return r


class _FakeTokens:
    async def get_token(self):
        return "tok"

    async def force_refresh(self):
        return "tok"


def test_get_retries_on_rate_limit_then_succeeds():
    rate = _Resp(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "slow down"}})
    ok = _Resp(200, {"files": [], "nextPageToken": None})
    api = DriveAPI(_FakeTokens(), lambda: [], backoffs=(0, 0, 0))
    api._client = _FakeClient([rate, ok])
    result = asyncio.run(api._get("http://x", {}))
    assert result == {"files": [], "nextPageToken": None}
    assert api._client.calls == 2  # retried once, did not crash


def test_get_raises_after_backoffs_exhausted():
    rate = _Resp(429, {"error": {"message": "too many"}})
    api = DriveAPI(_FakeTokens(), lambda: [], backoffs=(0,))
    api._client = _FakeClient([rate, rate])
    with pytest.raises(DriveAPIError):
        asyncio.run(api._get("http://x", {}))


# ------------------------------------------------------- scan end-to-end ------

class _FakeScanAPI:
    """Serves a synthetic folder tree; records that no real network is used."""

    def __init__(self, tree):
        self.tree = tree  # folder_id -> [raw file/folder dicts]
        self.seeded = []

    async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                     kinds=("video",)):
        key = folder_id or drive_id
        return {"files": self.tree.get(key, []), "nextPageToken": None}

    def seed_meta(self, meta):
        self.seeded.append(meta)


class _DisabledTMDB:
    enabled = False


def _cache(tmp_path):
    """A ScanCache stored in the test's tmp dir (shared within one test)."""
    from drivecast.scan_cache import ScanCache
    return ScanCache(path=str(tmp_path / "scan_cache.json"))


def test_scan_builds_library_without_network(tmp_path):
    tree = {
        "drv1": [
            rawfolder("movieF", "Arrival (2016)"),
            rawfolder("showF", "The Bear"),
            rawfile("loose", "Whiplash 2014 1080p.mkv", size=42, dur=6000),
        ],
        "movieF": [rawfile("mv", "Arrival.2016.1080p.mkv", size=9000, dur=7200000)],
        "showF": [rawfolder("s1", "Season 1")],
        "s1": [
            rawfile("s1e1", "The.Bear.S01E01.mkv", dur=1500000),
            rawfile("s1e2", "The.Bear.S01E02.mkv", dur=1600000),
        ],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"]))
    assert status["error"] is None
    assert status["running"] is False
    titles = {t["title"]: t for t in lib.titles_list()}
    assert set(titles) == {"Arrival", "The Bear", "Whiplash"}
    assert titles["Arrival"]["type"] == "movie"
    assert titles["The Bear"]["type"] == "show"
    assert len(titles["The Bear"]["seasons"][0]["episodes"]) == 2
    # File index / meta seeding populated for playback without a Drive call.
    assert lib.file_info("mv")["duration_ms"] == 7200000


def test_scan_survives_folder_rate_limit(tmp_path):
    class _FlakyAPI(_FakeScanAPI):
        async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                         kinds=("video",)):
            if folder_id == "badF":
                raise DriveAPIError(403, "rate", "rateLimitExceeded")
            return await super().browse(drive_id, folder_id, page_token, page_size)

    tree = {
        "drv1": [rawfolder("badF", "Broken"), rawfolder("goodF", "Arrival (2016)")],
        "goodF": [rawfile("mv", "Arrival.2016.mkv", size=10)],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FlakyAPI(tree), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"]))
    # A folder failure invalidates the whole drive's scan (a PARTIAL result
    # must never become the cached truth) — scan never crashes, error recorded.
    assert status["running"] is False
    assert lib.titles_list() == []           # nothing cached from a partial walk
    assert status["error"] is not None

    # Once the folder recovers, a rescan lands everything.
    tree_ok = {k: v for k, v in tree.items()}
    tree_ok["drv1"] = [rawfolder("goodF", "Arrival (2016)")]
    scanner2 = library.Scanner(_FakeScanAPI(tree_ok), _DisabledTMDB(), lib, throttle=0,
                               cache=_cache(tmp_path))
    asyncio.run(scanner2.scan(["drv1"]))
    assert [t["title"] for t in lib.titles_list()] == ["Arrival"]


# --------------------------------------------- drive thumbnail fallback --------

class _ThumbScanAPI(_FakeScanAPI):
    """Fake scan API that also serves thumbnail bytes and records fetches."""

    def __init__(self, tree):
        super().__init__(tree)
        self.thumb_fetches = []

    async def fetch_thumbnail(self, url):
        self.thumb_fetches.append(url)
        return b"jpeg-bytes"


class _FakeTMDB:
    """Enabled TMDB stub that always resolves a poster."""
    enabled = True

    async def enrich(self, title, year=None, media_type="movie"):
        return {"tmdb_id": 1, "title": title, "year": year,
                "poster_key": "tmdb.jpg", "overview": "o"}


def _thumb_tree():
    return {
        "drv1": [rawfolder("movieF", "Arrival (2016)")],
        "movieF": [rawfile("mv", "Arrival.2016.mkv", size=10,
                           thumb="https://lh3.example/thumb=s220")],
    }


def test_scan_falls_back_to_drive_thumbnail(tmp_path, monkeypatch):
    # TMDB disabled: the poster comes from the video's own Drive thumbnail,
    # downloaded into POSTERS_DIR under a stable dthumb_* key.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    api = _ThumbScanAPI(_thumb_tree())
    scanner = library.Scanner(api, _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"]))
    rec = lib.titles_list()[0]
    assert rec["poster"] and rec["poster"].startswith("dthumb_")
    assert os.path.exists(os.path.join(config.POSTERS_DIR, rec["poster"]))
    assert "_thumb" not in rec          # transient key never persisted
    assert api.thumb_fetches            # thumbnail actually fetched


def test_scan_prefers_tmdb_poster_over_drive_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    api = _ThumbScanAPI(_thumb_tree())
    scanner = library.Scanner(api, _FakeTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"]))
    rec = lib.titles_list()[0]
    assert rec["poster"] == "tmdb.jpg"
    assert api.thumb_fetches == []      # fallback never triggered


def test_scan_thumbnail_failure_leaves_poster_none(tmp_path, monkeypatch):
    class _NoThumbAPI(_ThumbScanAPI):
        async def fetch_thumbnail(self, url):
            return None

    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_NoThumbAPI(_thumb_tree()), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"]))
    assert status["error"] is None
    rec = lib.titles_list()[0]
    assert rec["poster"] is None
    assert "_thumb" not in rec


def test_rescan_upgrades_dthumb_to_tmdb_poster(tmp_path, monkeypatch):
    # First scan without TMDB leaves a dthumb fallback; enabling TMDB and
    # rescanning must upgrade to the real poster (and delete the old file).
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    rec = lib.titles_list()[0]
    old_file = os.path.join(config.POSTERS_DIR, rec["poster"])
    assert rec["poster"].startswith("dthumb_") and os.path.exists(old_file)

    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _FakeTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    rec = lib.titles_list()[0]
    assert rec["poster"] == "tmdb.jpg"
    assert not os.path.exists(old_file)  # superseded fallback cleaned up


def test_tmdb_match_without_artwork_keeps_dthumb(tmp_path, monkeypatch):
    class _NoArtTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            return {"tmdb_id": 7, "title": title, "year": year,
                    "poster_key": None, "overview": "o"}

    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    dthumb = lib.titles_list()[0]["poster"]
    assert dthumb.startswith("dthumb_")

    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _NoArtTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    rec = lib.titles_list()[0]
    assert rec["poster"] == dthumb          # fallback survives
    assert rec["tmdb_id"] == 7              # metadata still enriched


def test_rescan_restores_missing_dthumb_file(tmp_path, monkeypatch):
    # If the cached fallback file is deleted, a rescan re-downloads it instead
    # of leaving the record pointing at a 404ing key.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    poster_file = os.path.join(config.POSTERS_DIR, lib.titles_list()[0]["poster"])
    os.remove(poster_file)

    api = _ThumbScanAPI(_thumb_tree())
    asyncio.run(library.Scanner(api, _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"]))
    assert api.thumb_fetches                 # re-downloaded
    assert os.path.exists(poster_file)


def test_fetch_thumbnail_falls_back_when_bumped_request_raises():
    import httpx

    class _Img:
        status_code = 200
        content = b"img"

    class _FlakyThumbClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None, headers=None, follow_redirects=False):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("slow")   # bumped =s640 times out
            return _Img()

    api = DriveAPI(_FakeTokens(), lambda: [])
    client = _FlakyThumbClient()
    api._client = client
    data = asyncio.run(api.fetch_thumbnail("https://lh3.example/abc=s220"))
    assert data == b"img"
    assert client.calls[0].endswith("=s640")     # tried the bumped size first
    assert client.calls[1].endswith("=s220")     # then fell back to original


def test_title_for_file_maps_movies_and_episodes(tmp_path):
    lib = library.Library(path=str(tmp_path / "library.json"))
    lib.replace({
        "movieA": {"id": "movieA", "type": "movie", "title": "Arrival",
                   "file_id": "fA", "size": 1, "duration_ms": None},
        "showB": {"id": "showB", "type": "show", "title": "The Bear",
                  "seasons": [{"season": 1, "episodes": [
                      {"title": "System", "episode": 1, "file_id": "fE1",
                       "name": "e1.mkv", "duration_ms": None, "size": 1,
                       "parent_id": "s1"}]}]},
    })
    assert lib.title_for_file("fA")["id"] == "movieA"
    assert lib.title_for_file("fE1")["id"] == "showB"
    assert lib.title_for_file("nope") is None


# ------------------------------------------------- season grouping (v2) --------

def _showrec(fid, drive, folder_name, season, n_eps, year=None):
    """A classified show record for one season folder (with _folder_name)."""
    eps = [{"title": "Episode %d" % i, "episode": i, "file_id": "%se%d" % (fid, i),
            "name": "ep%d.mkv" % i, "duration_ms": None, "size": 1, "parent_id": fid}
           for i in range(1, n_eps + 1)]
    return {"id": fid, "type": "show", "title": folder_name, "year": year,
            "drive_id": drive, "folder_id": fid, "poster": None, "tmdb_id": None,
            "overview": None, "_folder_name": folder_name,
            "seasons": [{"season": season, "episodes": eps}]}


def test_group_prefixed_season_folders():
    # "Blackadder Season 1 S01" siblings -> one "Blackadder" show.
    recs = [_showrec("b1", "dBL", "Blackadder Season 1 S01", 1, 6),
            _showrec("b2", "dBL", "Blackadder Season 2 S02", 2, 6),
            _showrec("b3", "dBL", "Blackadder Specials", 0, 2)]
    out = library.group_seasons(recs, {"dBL": "TV | Blackadder"})
    assert len(out) == 1
    show = out[0]
    assert show["type"] == "show" and show["title"] == "Blackadder"
    assert sorted(s["season"] for s in show["seasons"]) == [0, 1, 2]
    assert show["id"].startswith("grp:")


def test_group_bare_season_folders_uses_drive_name():
    recs = [_showrec("f1", "dFR", "Season 1", 1, 24),
            _showrec("f2", "dFR", "Season 2", 2, 24)]
    out = library.group_seasons(recs, {"dFR": "Fraiser"})
    assert len(out) == 1 and out[0]["title"] == "Fraiser"
    assert sorted(s["season"] for s in out[0]["seasons"]) == [1, 2]


def test_group_merges_show_split_across_drives():
    # Malcolm in the Middle split into Part 1 / Part 2 drives, bare seasons.
    recs = [_showrec("a", "dM1", "Season 1", 1, 16),
            _showrec("b", "dM2", "Season 5", 5, 22)]
    names = {"dM1": "TV | Malcom in the Middle (Part 1)",
             "dM2": "TV | Malcom in the Middle (Part 2)"}
    out = library.group_seasons(recs, names)
    assert len(out) == 1
    assert out[0]["title"] == "Malcom in the Middle"
    assert sorted(s["season"] for s in out[0]["seasons"]) == [1, 5]


def test_group_leaves_range_named_wrapper_untouched():
    # "The Office Season 1-9 S01-s09" is a whole-series wrapper, not one season.
    office = {"id": "off", "type": "show", "title": "The Office", "year": 2005,
              "drive_id": "dTV", "folder_id": "off", "poster": None, "tmdb_id": None,
              "overview": None, "_folder_name": "The Office Season 1-9 S01-s09",
              "seasons": [{"season": n, "episodes": []} for n in range(1, 10)]}
    out = library.group_seasons([office], {"dTV": "TV Shows"})
    assert len(out) == 1 and out[0]["id"] == "off"
    assert "_folder_name" not in out[0]  # transient key stripped


def test_group_passthrough_strips_transient_keys():
    movie = {"id": "mv", "type": "movie", "title": "Arrival", "year": 2016,
             "drive_id": "d", "folder_id": "mv", "file_id": "mv", "size": 10,
             "duration_ms": None, "poster": None, "tmdb_id": None, "overview": None,
             "_folder_name": "Arrival (2016)", "_video_name": "Arrival.2016.mkv"}
    out = library.group_seasons([movie], {"d": "Movies"})
    assert len(out) == 1 and out[0]["id"] == "mv" and out[0]["type"] == "movie"
    assert "_folder_name" not in out[0] and "_video_name" not in out[0]


# --------------------------------------------------- per-drive refresh (M1) ----

def _two_drive_tree():
    """drv1: a movie; drv2: a movie. Independent content."""
    return {
        "drv1": [rawfolder("m1F", "Arrival (2016)")],
        "m1F": [rawfile("mv1", "Arrival.2016.1080p.mkv", size=10)],
        "drv2": [rawfolder("m2F", "Sicario (2015)")],
        "m2F": [rawfile("mv2", "Sicario.2015.1080p.mkv", size=10)],
    }


def test_partial_refresh_equals_full_refresh(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"]))
    full = {t["id"]: t for t in lib.titles_list()}
    assert set(full) == {"mv1", "mv2"}

    # Add a movie to drv2, then refresh ONLY drv2.
    tree["drv2"].append(rawfolder("m3F", "Dune (2021)"))
    tree["m3F"] = [rawfile("mv3", "Dune.2021.2160p.mkv", size=10)]
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"]))
    titles = {t["id"]: t for t in lib.titles_list()}
    # drv1's title survives untouched; drv2's addition landed.
    assert set(titles) == {"mv1", "mv2", "mv3"}
    assert scanner.status["scope"] == ["drv2"]
    assert scanner.status["total"] == 1


def test_partial_refresh_preserves_cross_drive_grouped_show(tmp_path):
    # A show split across two "Part" drives groups into one grp: record; a
    # partial refresh of either drive must keep BOTH seasons.
    tree = {
        "dM1": [rawfolder("s1F", "Season 1")],
        "s1F": [rawfile("m1e1", "ep1.mkv"), rawfile("m1e2", "ep2.mkv")],
        "dM2": [rawfolder("s5F", "Season 5")],
        "s5F": [rawfile("m5e1", "ep1.mkv")],
    }

    class _NamedAPI(_FakeScanAPI):
        async def list_drives(self, force=False):
            return [{"id": "dM1", "name": "TV | Malcom in the Middle (Part 1)"},
                    {"id": "dM2", "name": "TV | Malcom in the Middle (Part 2)"}]

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_NamedAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["dM1", "dM2"]))
    rec = only(lib.titles_list())
    assert rec["id"].startswith("grp:")
    assert sorted(s["season"] for s in rec["seasons"]) == [1, 5]
    assert set(rec["source_drives"]) == {"dM1", "dM2"}
    grp_id = rec["id"]

    # Refresh only Part 2: the grouped show keeps both seasons, same id.
    asyncio.run(scanner.scan(["dM1", "dM2"], scope=["dM2"]))
    rec2 = only(lib.titles_list())
    assert rec2["id"] == grp_id
    assert sorted(s["season"] for s in rec2["seasons"]) == [1, 5]


def test_scan_failure_keeps_previous_titles(tmp_path):
    tree = _two_drive_tree()

    class _FailingRootAPI(_FakeScanAPI):
        fail_drive = None

        async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                         kinds=("video",)):
            if self.fail_drive and (folder_id or drive_id) == self.fail_drive:
                raise DriveAPIError(500, "boom", "backendError")
            return await super().browse(drive_id, folder_id, page_token, page_size)

    api = _FailingRootAPI(tree)
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(api, _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"]))
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}

    # drv1's root now errors: a rescan must NOT drop drv1's titles.
    api.fail_drive = "drv1"
    asyncio.run(scanner.scan(["drv1", "drv2"]))
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}
    assert scanner.status["error"] is not None


def test_partial_scope_escalates_when_sibling_cache_missing(tmp_path):
    # Fresh cache + a scoped request: scanning only the scope would drop every
    # uncached drive's titles, so the scan escalates to all selected drives.
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"]))
    assert scanner.status["total"] == 2                    # escalated to full
    assert set(scanner.status["scope"]) == {"drv1", "drv2"}
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}


def test_deselected_drive_pruned_from_cache_and_library(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    cache = _cache(tmp_path)
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=cache)
    asyncio.run(scanner.scan(["drv1", "drv2"]))
    assert set(cache.drive_ids()) == {"drv1", "drv2"}

    # Deselect drv2: its titles and cache entry disappear.
    asyncio.run(scanner.scan(["drv1"]))
    assert {t["id"] for t in lib.titles_list()} == {"mv1"}
    assert cache.drive_ids() == ["drv1"]


def test_added_at_stable_across_partial_refresh(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"]))
    first = lib.get("mv1")["added_at"]
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"]))
    assert lib.get("mv1")["added_at"] == first


def test_scan_cache_get_returns_deep_copies(tmp_path):
    from drivecast.scan_cache import ScanCache
    cache = ScanCache(path=str(tmp_path / "sc.json"))
    cache.put("d1", [{"id": "a", "seasons": [{"season": 1, "episodes": []}]}])
    got = cache.get("d1")
    got[0]["seasons"][0]["season"] = 99      # mutate the copy
    assert cache.get("d1")[0]["seasons"][0]["season"] == 1   # cache pristine


# ------------------------------------------------------- categorization (M2) --

def test_category_for_matrix():
    from drivecast import sections
    # No TMDB match -> other, unless a drive hint overrides.
    assert sections.category_for(None, "movie") == "other"
    assert sections.category_for(None, "show", "documentary") == "documentary"
    # Genre 99 -> documentary regardless of structure.
    assert sections.category_for({"genre_ids": [18, 99]}, "movie") == "documentary"
    assert sections.category_for({"genre_ids": [99]}, "show") == "documentary"
    # Otherwise the structural type.
    assert sections.category_for({"genre_ids": [35]}, "show") == "show"
    assert sections.category_for({"genre_ids": []}, "movie") == "movie"


def test_tmdb_cache_heal_drops_genreless_entries():
    from drivecast.tmdb import TMDB
    healed = TMDB._heal_cache({
        "movie|old|2000": {"tmdb_id": 1, "poster_key": "p.jpg"},   # pre-genres
        "movie|new|2020": {"tmdb_id": 2, "genre_ids": [28]},
        "movie|miss|": None,                                        # negative marker
    })
    assert "movie|old|2000" not in healed
    assert healed["movie|new|2020"]["tmdb_id"] == 2
    assert "movie|miss|" in healed and healed["movie|miss|"] is None


class _GenreTMDB:
    """TMDB stub: 'India The Modi Question' is a documentary; others match
    plain movies; 'Unknown' has no TMDB entry at all."""
    enabled = True

    async def enrich(self, title, year=None, media_type="movie"):
        if "modi" in title.lower():
            return {"tmdb_id": 9, "title": title, "year": year,
                    "poster_key": None, "overview": None, "genre_ids": [99]}
        if "unknown" in title.lower():
            return None
        return {"tmdb_id": 5, "title": title, "year": year,
                "poster_key": None, "overview": None, "genre_ids": [35]}


def test_scanner_stamps_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {
        "drv1": [rawfolder("docF", "India The Modi Question"),
                 rawfolder("movF", "Arrival (2016)"),
                 rawfolder("unkF", "Unknown Home Video")],
        "docF": [rawfile("d1", "India.The.Modi.Question.S01E01.mkv"),
                 rawfile("d2", "India.The.Modi.Question.S01E02.mkv")],
        "movF": [rawfile("m1", "Arrival.2016.1080p.mkv")],
        "unkF": [rawfile("u1", "Unknown Home Video.mp4")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"]))
    cats = {t["title"]: t["category"] for t in lib.titles_list()}
    assert cats["India The Modi Question"] == "documentary"
    assert cats["Arrival"] == "movie"
    assert cats["Unknown Home Video"] == "other"


def test_scanner_category_hint_overrides_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {"drv1": [rawfolder("unkF", "Unknown Nature Film")],
            "unkF": [rawfile("u1", "Unknown Nature Film.mp4")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"],
                             drive_hints={"drv1": {"category": "documentary"}}))
    assert only(lib.titles_list())["category"] == "documentary"


def test_category_carried_across_rescans(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {"drv1": [rawfolder("movF", "Arrival (2016)")],
            "movF": [rawfile("m1", "Arrival.2016.mkv")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"]))
    assert only(lib.titles_list())["category"] == "movie"
    # Rescan with TMDB disabled: the category is carried, not wiped.
    scanner2 = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                               cache=_cache(tmp_path))
    asyncio.run(scanner2.scan(["drv1"]))
    assert only(lib.titles_list())["category"] == "movie"


def test_category_null_when_tmdb_disabled(tmp_path):
    tree = {"drv1": [rawfolder("movF", "Arrival (2016)")],
            "movF": [rawfile("m1", "Arrival.2016.mkv")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"]))
    assert only(lib.titles_list())["category"] is None


# ------------------------------------------------------- sections (M3) --------

def test_migrate_library_v1_stamps_fields(tmp_path):
    import json as _json
    v1 = {"version": 1, "generated_at": 1.0, "titles": {
        "movieA": {"id": "movieA", "type": "movie", "title": "Arrival",
                   "drive_id": "drv1", "file_id": "fA", "size": 1},
    }}
    p = tmp_path / "library.json"
    p.write_text(_json.dumps(v1))
    lib = library.Library(path=str(p), drive_sections={"drv1": "courses"})
    rec = lib.get("movieA")
    assert lib.data["version"] == library.LIBRARY_VERSION
    assert rec["section"] == "courses"
    assert rec["category"] is None
    assert rec["media"] == "video"
    assert rec["source_drives"] == ["drv1"]
    # ids unchanged; file index still works
    assert lib.title_for_file("fA")["id"] == "movieA"


def test_scan_stamps_section_and_skips_tmdb_for_non_entertainment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {"drv1": [rawfolder("cF", "Intercourse and Communication")],
            "cF": [rawfile("c1", "1 - Tactical Empathy.mp4"),
                   rawfile("c2", "2 - Mirroring.mp4")]}

    calls = []

    class _SpyTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            calls.append(title)
            return {"tmdb_id": 1, "title": title, "year": year,
                    "poster_key": "wrong.jpg", "overview": None, "genre_ids": []}

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _SpyTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections={"drv1": "courses"}))
    recs = lib.titles_list()
    assert recs and all(r["section"] == "courses" for r in recs)
    assert calls == []                      # TMDB never consulted for courses
    assert all(not r.get("poster") for r in recs)   # no film posters attached


def test_course_named_part1_never_merges_as_season(tmp_path):
    # An entertainment drive has "X Season 1"-style folders; a COURSES drive
    # has a "(Part 1)" folder — grouping must only appl to entertainment.
    tree = {
        "ent": [rawfolder("s1", "Blackadder Season 1 S01")],
        "s1": [rawfile("e1", "ep1.mkv"), rawfile("e2", "ep2.mkv")],
        "crs": [rawfolder("p1", "Python Data Science (Part 1)")],
        "p1": [rawfile("l1", "01) Intro.mp4"), rawfile("l2", "02) Lists.mp4")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["ent", "crs"], drive_sections={"crs": "courses"}))
    secs = {t["title"]: t["section"] for t in lib.titles_list()}
    assert any(s == "courses" for s in secs.values())
    course = next(t for t in lib.titles_list() if t["section"] == "courses")
    assert not course["id"].startswith("grp:")   # never season-grouped


def test_scan_dispatches_course_and_plugin_classifiers(tmp_path, monkeypatch):
    # End-to-end through Scanner: a courses drive uses the course classifier,
    # and a drive assigned to a CUSTOM plugin section uses its classifier.
    from drivecast import sections as sections_mod

    def _plugin_classify(drive_id, drive_name, nodes, loose):
        # A tiny audio-series classifier: volume folders -> named seasons.
        seasons = []
        for node in nodes:
            for i, sf in enumerate(node["subfolders"]):
                eps = [{"title": v["name"], "episode": j + 1,
                        "file_id": v["id"], "name": v["name"],
                        "duration_ms": v.get("duration_ms"), "size": v.get("size"),
                        "parent_id": v.get("parent_id"), "media": "audio"}
                       for j, v in enumerate(sf["videos"])]
                seasons.append({"season": i + 1, "name": sf["name"], "episodes": eps})
            if seasons:
                return [{"id": node["id"], "type": "show", "title": node["name"],
                         "year": None, "drive_id": drive_id, "folder_id": node["id"],
                         "poster": None, "tmdb_id": None, "overview": None,
                         "quality": None, "shelf": "Series", "media": "audio",
                         "_thumb": None, "seasons": seasons}]
        return []

    monkeypatch.setattr(sections_mod, "_plugins", {
        "myaudio": {"key": "myaudio", "label": "My Audio", "icon": "♪",
                    "mimes": ("video", "audio"), "classify": _plugin_classify},
    })

    tree = {
        # courses drive: one flat course with two numbered lessons + workbook
        "crs": [rawfolder("courseF", "Art_of_Negotiation")],
        "courseF": [
            rawfile("l1", "1 - Tactical Empathy.mp4", dur=600000),
            rawfile("l2", "2 - Mirroring.mp4", dur=650000),
            rawfile("wb", "Class Workbook.pdf", mime="application/pdf"),
        ],
        # plugin drive: one series folder with one volume of audio tracks
        "aud": [rawfolder("seriesF", "Morning Series")],
        "seriesF": [rawfolder("v1F", "Volume 01")],
        "v1F": [rawfile("t1", "01 Track One.mp3", mime="audio/mpeg"),
                rawfile("t2", "02 Track Two.mp3", mime="audio/mpeg")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["crs", "aud"],
                             drive_sections={"crs": "courses", "aud": "myaudio"}))
    by_section = {}
    for t in lib.titles_list():
        by_section.setdefault(t["section"], []).append(t)

    course = only(by_section["courses"])
    assert course["type"] == "show"
    assert course["title"] == "Art of Negotiation"
    eps = course["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2]
    assert course["materials"] and course["materials"][0]["name"] == "Class Workbook.pdf"

    series = only(by_section["myaudio"])
    assert series["media"] == "audio"
    assert series["shelf"] == "Series"
    vol = series["seasons"][0]
    assert vol["name"] == "Volume 01"
    assert [e["episode"] for e in vol["episodes"]] == [1, 2]
    assert all(e.get("media") == "audio" for e in vol["episodes"])
    # plugin episodes indexed for playback + Continue enrichment
    assert lib.title_for_file("t1")["id"] == series["id"]


def test_plugin_loading_from_dir(tmp_path, monkeypatch):
    # A SECTION plugin dropped into the plugin dir loads; junk files are
    # ignored and a broken plugin never crashes the app.
    from drivecast import sections as sections_mod
    plug_dir = tmp_path / "sections"
    plug_dir.mkdir()
    (plug_dir / "custom.py").write_text(
        "def classify(drive_id, drive_name, nodes, loose):\n"
        "    return []\n"
        "SECTION = {'key': 'custom', 'label': 'Custom', 'icon': '*',\n"
        "           'mimes': ('video',), 'classify': classify}\n")
    (plug_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    (plug_dir / "notaplugin.py").write_text("x = 1\n")
    monkeypatch.setattr(sections_mod, "PLUGIN_DIR", str(plug_dir))
    monkeypatch.setattr(sections_mod, "_plugins", None)   # reset the cache
    assert "custom" in sections_mod.all_sections()
    assert sections_mod.mimes_for("custom") == ("video",)
    assert callable(sections_mod.classify_for("custom"))
    assert sections_mod.classify_for("entertainment") is None
    metas = {m["key"] for m in sections_mod.meta_list()}
    assert metas == {"entertainment", "courses", "podcasts", "custom"}
    monkeypatch.setattr(sections_mod, "_plugins", None)   # don't leak to other tests
