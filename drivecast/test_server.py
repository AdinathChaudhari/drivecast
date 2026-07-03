"""API tests that serve entirely from a synthetic cache — no Drive network.

The DriveAPI's low-level GET is stubbed to raise, so any test that accidentally
reaches the Drive API fails loudly. Playback and library/settings endpoints must
answer from the cached library alone.
"""
import json

import pytest
from fastapi.testclient import TestClient

from drivecast import config as config_mod
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
            "drive_id": "drv1", "folder_id": "showB", "poster": None,
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
    monkeypatch.setattr(server_mod, "Library", lambda: library_mod.Library(path=str(lib_path)))

    # No rclone / no Drive network anywhere.
    async def _fake_token(self):
        return "faketoken"

    def _no_network(self, url, params):
        raise AssertionError("Drive API was contacted: %s" % url)

    captured = {}

    def _fake_play(self, file_id, name, duration_ms=None, drive_id=None, parent_id=None):
        captured["file_id"] = file_id
        captured["duration_ms"] = duration_ms
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


def test_refresh_status_shape(client):
    r = client.get("/api/refresh/status")
    assert r.status_code == 200
    st = r.json()
    for k in ("running", "scanned", "total", "added", "removed", "error"):
        assert k in st
