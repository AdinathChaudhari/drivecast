"""API tests that serve entirely from a synthetic cache — no Drive network.

The DriveAPI's low-level GET is stubbed to raise, so any test that accidentally
reaches the Drive API fails loudly. Playback and library/settings endpoints must
answer from the cached library alone.
"""
import json

import pytest
from fastapi.testclient import TestClient

from drivecast import config as config_mod
from drivecast import history as history_mod
from drivecast import library as library_mod
from drivecast import server as server_mod
from drivecast.drive_api import DriveAPI
from drivecast.player import PlayerManager
from drivecast.rclone_auth import TokenManager


SYNTHETIC = {
    "version": 1,
    "generated_at": 123.0,
    "titles": {
        "movieA": {
            "id": "movieA", "type": "movie", "title": "Arrival", "year": 2016,
            "drive_id": "drv1", "folder_id": "movieA", "poster": None,
            "tmdb_id": None, "overview": "aliens", "quality": "4K",
            "file_id": "fileA", "size": 5000, "duration_ms": 7200000,
        },
        "showB": {
            "id": "showB", "type": "show", "title": "The Bear", "year": 2022,
            "drive_id": "drv1", "folder_id": "showB", "poster": "bear.jpg",
            "tmdb_id": None, "overview": "kitchen",
            "seasons": [{"season": 1, "episodes": [
                {"title": "System", "episode": 1, "file_id": "fileE1",
                 "name": "The.Bear.S01E01.mkv", "duration_ms": 1500000,
                 "size": 900, "parent_id": "s1"},
            ]}],
        },
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Write a synthetic library the server will load.
    lib_path = tmp_path / "library.json"
    lib_path.write_text(json.dumps(SYNTHETIC))

    # Point the server's Library/Scanner at the temp file.
    monkeypatch.setattr(server_mod, "Library",
                        lambda **kw: library_mod.Library(path=str(lib_path), **kw))

    # Keep watch history and the scan cache in the temp dir too — never touch
    # the user's real data.
    monkeypatch.setattr(server_mod, "History",
                        lambda: history_mod.History(path=str(tmp_path / "history.json")))
    from drivecast import scan_cache as scan_cache_mod
    monkeypatch.setattr(server_mod, "ScanCache",
                        lambda: scan_cache_mod.ScanCache(path=str(tmp_path / "scan_cache.json")))

    # No rclone / no Drive network anywhere.
    async def _fake_token(self):
        return "faketoken"

    def _no_network(self, url, params):
        raise AssertionError("Drive API was contacted: %s" % url)

    captured = {}

    def _fake_play(self, file_id, name, duration_ms=None, drive_id=None,
                   parent_id=None, queue=None, media=None, sub_path=None):
        captured["media"] = media
        captured["sub_path"] = sub_path
        captured["file_id"] = file_id
        captured["duration_ms"] = duration_ms
        captured["queue"] = queue
        return {"player": "mpv", "resumed_from": 0}

    monkeypatch.setattr(TokenManager, "get_token", _fake_token)
    monkeypatch.setattr(DriveAPI, "_get", _no_network)
    monkeypatch.setattr(PlayerManager, "play", _fake_play)
    monkeypatch.setattr(config_mod, "save_config", lambda cfg: None)

    cfg = dict(config_mod.DEFAULTS)
    cfg.update({"tmdb_api_key": "", "selected_drives": ["drv1"],
                "auto_refresh_on_startup": False})

    app = server_mod.create_app(cfg)
    with TestClient(app) as c:
        # Keep the subtitle cache in the temp dir; Drive lookups already fail
        # loudly via the _no_network stub.
        c.app.state.dc.subtitles.subs_dir = str(tmp_path / "subs")
        c._captured = captured
        yield c


def test_library_endpoint_serves_cache(client):
    r = client.get("/api/library")
    assert r.status_code == 200
    data = r.json()
    titles = {t["title"] for t in data["titles"]}
    assert titles == {"Arrival", "The Bear"}
    assert data["selected_drives"] == ["drv1"]
    assert data["scanning"] is False
    # Quality field is serialized straight through from the record.
    arrival = next(t for t in data["titles"] if t["title"] == "Arrival")
    assert arrival["quality"] == "4K"


def test_watched_map_endpoint(client):
    # Record a play position, then the watched-map exposes its last_played.
    client.post("/api/play", json={"file_id": "fileA", "name": "Arrival"})
    client.app.state.dc.history.update("fileA", position=120.0, duration=7200.0, force=True)
    r = client.get("/api/watched-map")
    assert r.status_code == 200
    m = r.json()["map"]
    assert "fileA" in m and m["fileA"] > 0


def test_title_endpoint(client):
    r = client.get("/api/title/showB")
    assert r.status_code == 200
    rec = r.json()
    assert rec["type"] == "show"
    assert rec["seasons"][0]["episodes"][0]["title"] == "System"

    assert client.get("/api/title/nope").status_code == 404


def test_settings_get(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["selected_drives"] == ["drv1"]
    assert body["auto_refresh_on_startup"] is False
    assert "player" in body            # player preference exposed
    assert "available_players" in body  # which players are installed


def test_settings_post_player(client):
    r = client.post("/api/settings", json={"player": "vlc"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["player"] == "vlc"
    # invalid choice is ignored (stays a valid value)
    client.post("/api/settings", json={"player": "bogus"})
    assert client.get("/api/settings").json()["player"] == "vlc"


def test_settings_post_toggle_auto_refresh(client):
    r = client.post("/api/settings", json={"auto_refresh_on_startup": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["auto_refresh_on_startup"] is True
    # No drive change -> no refresh kicked.
    assert body["refresh_started"] is False


def test_play_uses_cached_duration_no_drive_call(client):
    # POST with only a file_id: duration must come from the cached library,
    # and no Drive metadata call may happen (DriveAPI._get raises if it does).
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Arrival"})
    assert r.status_code == 200
    assert r.json()["player"] == "mpv"
    assert client._captured["duration_ms"] == 7200000  # from the cache


def test_play_episode_cached_duration(client):
    r = client.post("/api/play", json={"file_id": "fileE1", "name": "Ep1"})
    assert r.status_code == 200
    assert client._captured["duration_ms"] == 1500000


def test_play_passes_queue_through(client):
    # An autoplay queue is whitelisted and handed to PlayerManager.play.
    r = client.post("/api/play", json={
        "file_id": "fileE1", "name": "Ep1",
        "queue": [
            {"file_id": "fileE2", "name": "Ep2", "duration_ms": 1200000},
            {"file_id": "fileE3", "name": "Ep3"},
        ],
    })
    assert r.status_code == 200
    q = client._captured["queue"]
    assert [x["file_id"] for x in q] == ["fileE2", "fileE3"]
    assert q[0]["duration_ms"] == 1200000
    assert q[0]["name"] == "Ep2"


def test_play_queue_drops_malformed_items(client):
    # Items without a file_id (or non-dicts) are dropped; a non-list is ignored.
    r = client.post("/api/play", json={
        "file_id": "fileE1", "name": "Ep1",
        "queue": [{"name": "no id"}, "garbage", {"file_id": "fileE2"}],
    })
    assert r.status_code == 200
    assert [x["file_id"] for x in client._captured["queue"]] == ["fileE2"]

    r2 = client.post("/api/play", json={"file_id": "fileE1", "name": "Ep1", "queue": "nope"})
    assert r2.status_code == 200
    assert client._captured["queue"] == []


def test_settings_roundtrips_autoplay_next(client):
    # Default is on; POST toggles it off and GET reflects the change.
    assert client.get("/api/settings").json()["autoplay_next"] is True
    r = client.post("/api/settings", json={"autoplay_next": False})
    assert r.status_code == 200
    assert r.json()["autoplay_next"] is False
    assert client.get("/api/settings").json()["autoplay_next"] is False


def test_continue_enriched_with_library_title(client):
    # A partially-watched episode surfaces on the Continue shelf carrying the
    # owning show's title/poster so the UI can render a thumbnail.
    client.app.state.dc.history.update("fileE1", name="The.Bear.S01E01.mkv",
                                       position=600.0, duration=1500.0, force=True)
    r = client.get("/api/continue")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["file_id"] == "fileE1"
    assert it["title"] == "The Bear"
    assert it["title_id"] == "showB"
    assert it["type"] == "show"
    assert it["poster"] == "bear.jpg"


def test_continue_unknown_file_passes_through(client):
    # Files played outside the library (e.g. via Browse) stay unenriched.
    client.app.state.dc.history.update("strayFile", name="stray.mkv",
                                       position=100.0, duration=1000.0, force=True)
    items = client.get("/api/continue").json()["items"]
    it = next(x for x in items if x["file_id"] == "strayFile")
    assert it["name"] == "stray.mkv"
    assert "poster" not in it and "title" not in it


def test_refresh_status_shape(client):
    r = client.get("/api/refresh/status")
    assert r.status_code == 200
    st = r.json()
    for k in ("running", "scanned", "total", "added", "removed", "error",
              "scope", "scope_names"):
        assert k in st


def _capture_refresh(client):
    """Stub start_refresh on the live AppState, capturing the scope."""
    calls = []

    def _fake(scope=None):
        calls.append(scope)
        return True

    client.app.state.dc.start_refresh = _fake
    return calls


def test_refresh_scoped_to_one_drive(client):
    calls = _capture_refresh(client)
    r = client.post("/api/refresh", json={"drives": ["drv1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["scope"] == ["drv1"]
    assert calls == [["drv1"]]


def test_refresh_bodyless_is_full(client):
    # The menubar POSTs with no body: full refresh over all selected drives.
    calls = _capture_refresh(client)
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert r.json()["scope"] == ["drv1"]
    assert calls == [None]


def test_refresh_rejects_unselected_drive(client):
    calls = _capture_refresh(client)
    r = client.post("/api/refresh", json={"drives": ["not-selected"]})
    assert r.status_code == 400
    assert calls == []

    r2 = client.post("/api/refresh", json={"drives": "drv1"})   # not a list
    assert r2.status_code == 400


def test_settings_drive_sections_roundtrip_and_scoped_refresh(client):
    calls = _capture_refresh(client)
    r = client.post("/api/settings", json={"drive_sections": {"drv1": "podcasts", "x": "bogus"}})
    assert r.status_code == 200
    body = r.json()
    assert body["drive_sections"] == {"drv1": "podcasts"}   # invalid value dropped
    # Changing drv1's section triggered a refresh scoped to just drv1.
    assert calls == [["drv1"]]
    assert client.get("/api/settings").json()["drive_sections"] == {"drv1": "podcasts"}


def test_watched_map_progress_shape(client):
    client.app.state.dc.history.update("fileA", position=3600.0, duration=7200.0, force=True)
    body = client.get("/api/watched-map").json()
    assert "map" in body and "progress" in body
    p = body["progress"]["fileA"]
    assert p["percent"] == 50.0 and p["watched"] is False


def test_play_resolves_and_passes_subtitle(client, tmp_path):
    async def _fake_resolve(file_id, name, drive_id=None, parent_id=None):
        return str(tmp_path / "subs" / ("%s.srt" % file_id))

    client.app.state.dc.subtitles.resolve = _fake_resolve
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Arrival"})
    assert r.status_code == 200
    assert r.json()["subtitles"] is True
    assert client._captured["sub_path"].endswith("fileA.srt")


def test_play_subtitles_toggle_off(client):
    calls = []

    async def _spy_resolve(*a, **k):
        calls.append(a)
        return None

    client.app.state.dc.subtitles.resolve = _spy_resolve
    client.post("/api/settings", json={"subtitles": False})
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Arrival"})
    assert r.status_code == 200
    assert r.json()["subtitles"] is False
    assert calls == []                     # resolver never consulted
    assert client._captured["sub_path"] is None


def test_settings_roundtrips_subtitles(client):
    assert client.get("/api/settings").json()["subtitles"] is True
    client.post("/api/settings", json={"subtitles": False})
    assert client.get("/api/settings").json()["subtitles"] is False
