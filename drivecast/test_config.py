"""Tests for config: user-dir relocation, secret resolution, saved-key policy.

All synthetic — paths are monkeypatched into a tmp dir so the real
~/Library/Application Support/drivecast is never touched.
"""
import json
import os

from drivecast import config


def _redirect(monkeypatch, tmp_path):
    """Point every config path at a throwaway USER_DIR under tmp_path."""
    user_dir = tmp_path / "drivecast"
    data_dir = user_dir / "data"
    secrets_dir = user_dir / "secrets"
    monkeypatch.setattr(config, "USER_DIR", str(user_dir))
    monkeypatch.setattr(config, "CONFIG_PATH", str(user_dir / "config.json"))
    monkeypatch.setattr(config, "SECRETS_PATH", str(secrets_dir / "secrets.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "POSTERS_DIR", str(data_dir / "posters"))
    monkeypatch.delenv("DRIVECAST_TMDB_API_KEY", raising=False)
    return user_dir, data_dir, secrets_dir


def test_tmdb_api_key_is_secret_not_saved():
    assert "tmdb_api_key" in config.SECRET_KEYS
    assert "tmdb_api_key" not in config.SAVED_KEYS


def test_secret_resolves_from_user_dir(tmp_path, monkeypatch):
    user_dir, _, secrets_dir = _redirect(monkeypatch, tmp_path)
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.json").write_text(json.dumps({"tmdb_api_key": "SEKRET"}))
    user_dir.mkdir(exist_ok=True)
    (user_dir / "config.json").write_text(json.dumps({"selected_drives": ["d1"]}))

    cfg = config.load_config()
    assert cfg["tmdb_api_key"] == "SEKRET"        # from USER_DIR/secrets
    assert cfg["selected_drives"] == ["d1"]       # from USER_DIR/config.json


def test_env_var_overrides_secrets_file(tmp_path, monkeypatch):
    user_dir, _, secrets_dir = _redirect(monkeypatch, tmp_path)
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.json").write_text(json.dumps({"tmdb_api_key": "FROMFILE"}))
    monkeypatch.setenv("DRIVECAST_TMDB_API_KEY", "FROMENV")

    cfg = config.load_config()
    assert cfg["tmdb_api_key"] == "FROMENV"


def test_load_creates_dirs_and_config_from_example(tmp_path, monkeypatch):
    user_dir, data_dir, _ = _redirect(monkeypatch, tmp_path)
    # No config.json yet -> first run copies from the repo example.
    cfg = config.load_config()
    assert os.path.isdir(str(data_dir))
    assert os.path.isdir(str(data_dir / "posters"))
    assert os.path.exists(str(user_dir / "config.json"))
    assert "tmdb_api_key" in cfg  # resolved (empty) even with no secrets file


def test_save_config_never_writes_secret(tmp_path, monkeypatch):
    user_dir, _, _ = _redirect(monkeypatch, tmp_path)
    user_dir.mkdir(parents=True)
    config.save_config({"selected_drives": ["x"], "tmdb_api_key": "SHOULD_NOT_PERSIST",
                        "port": 9000})
    on_disk = json.loads((user_dir / "config.json").read_text())
    assert on_disk["selected_drives"] == ["x"]
    assert on_disk["port"] == 9000
    assert "tmdb_api_key" not in on_disk
