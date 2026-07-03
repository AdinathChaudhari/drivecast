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
    return {"id": fid, "name": name, "drive_id": drive,
            "videos": list(videos), "subfolders": list(subfolders)}


def rawfile(fid, name, mime="video/mp4", size=1000, dur=None):
    f = {"id": fid, "name": name, "mimeType": mime, "size": str(size)}
    if dur is not None:
        f["videoMediaMetadata"] = {"durationMillis": str(dur)}
    return f


def rawfolder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


# ------------------------------------------------------- classify: movie ------

def test_classify_movie_folder():
    n = node("Your Name (2016) [BluRay] [1080p]",
             [vid("v1", "Your.Name.2016.1080p.BluRay.mkv", size=5000, dur=360000)])
    rec = library.classify_title(n)
    assert rec["type"] == "movie"
    assert rec["title"] == "Your Name"
    assert rec["year"] == 2016
    assert rec["file_id"] == "v1"
    assert rec["duration_ms"] == 360000
    assert rec["size"] == 5000


def test_movie_picks_largest_and_ignores_samples():
    n = node("Inception (2010)", [
        vid("s", "Inception-sample.mkv", size=9999),        # sample: excluded
        vid("t", "Inception-trailer.mp4", size=8888),       # trailer: excluded
        vid("main", "Inception.2010.1080p.mkv", size=5000),
        vid("small", "Inception.2010.480p.mkv", size=1000),
    ])
    rec = library.classify_title(n)
    assert rec["type"] == "movie"
    assert rec["file_id"] == "main"  # largest non-sample


def test_empty_folder_returns_none():
    assert library.classify_title(node("Empty", [])) is None
    # Folder with only a sample is effectively empty.
    assert library.classify_title(node("OnlySample", [vid("s", "movie-sample.mkv")])) is None


# ------------------------------------------------------- classify: show -------

def test_show_by_season_subfolders():
    videos = [
        vid("e1", "Ep 01.mkv", parent="s1", ancestors=["Season 1"]),
        vid("e2", "Ep 02.mkv", parent="s1", ancestors=["Season 1"]),
        vid("e3", "Ep 01.mkv", parent="s2", ancestors=["Season 2"]),
    ]
    rec = library.classify_title(node("The Bear", videos, subfolders=["Season 1", "Season 2"]))
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
    rec = library.classify_title(node("Breaking Bad", videos))
    assert rec["type"] == "show"
    eps = rec["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2, 10]  # sorted by episode number
    assert rec["seasons"][0]["season"] == 5


def test_single_episode_is_still_a_movie_heuristic():
    # One episode-named file and no season folder -> falls through to movie.
    rec = library.classify_title(node("Random", [vid("v", "Random.S01E01.mkv")]))
    assert rec["type"] == "movie"


def test_episode_title_extracted():
    videos = [
        vid("a", "Frasier (1993) - S05E10 - Where Every Bloke.mkv"),
        vid("b", "Frasier (1993) - S05E11 - Perspectives.mkv"),
    ]
    rec = library.classify_title(node("Frasier", videos))
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

    async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200):
        key = folder_id or drive_id
        return {"files": self.tree.get(key, []), "nextPageToken": None}

    def seed_meta(self, meta):
        self.seeded.append(meta)


class _DisabledTMDB:
    enabled = False


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
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0)
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
        async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200):
            if folder_id == "badF":
                raise DriveAPIError(403, "rate", "rateLimitExceeded")
            return await super().browse(drive_id, folder_id, page_token, page_size)

    tree = {
        "drv1": [rawfolder("badF", "Broken"), rawfolder("goodF", "Arrival (2016)")],
        "goodF": [rawfile("mv", "Arrival.2016.mkv", size=10)],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FlakyAPI(tree), _DisabledTMDB(), lib, throttle=0)
    status = asyncio.run(scanner.scan(["drv1"]))
    # The bad folder is skipped, the good one still lands — scan never crashes.
    assert status["running"] is False
    titles = [t["title"] for t in lib.titles_list()]
    assert "Arrival" in titles
    assert status["error"] is not None  # recorded the skipped folder


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
