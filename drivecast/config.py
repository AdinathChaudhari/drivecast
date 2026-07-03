"""Configuration: merge config.json over defaults, auto-create on first run."""
import json
import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
EXAMPLE_PATH = os.path.join(REPO_ROOT, "config.example.json")
DATA_DIR = os.path.join(REPO_ROOT, "data")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")

DEFAULTS = {
    "remote": "gdrive1",
    "tmdb_api_key": "",
    "player": "auto",
    "port": 8737,
    "page_size": 200,
}


def _ensure_config_file():
    """Create config.json from config.example.json on first run."""
    if os.path.exists(CONFIG_PATH):
        return
    try:
        if os.path.exists(EXAMPLE_PATH):
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            with open(CONFIG_PATH, "w") as f:
                json.dump(DEFAULTS, f, indent=2)
    except OSError:
        pass


def load_config():
    """Return merged config (config.json over DEFAULTS). Creates dirs as needed."""
    _ensure_config_file()
    merged = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            merged.update(json.load(f))
    except (OSError, ValueError):
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(POSTERS_DIR, exist_ok=True)
    return merged
