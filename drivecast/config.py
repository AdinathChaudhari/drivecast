"""Configuration: merge config.json over defaults, auto-create on first run."""
import json
import os
import shutil
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
EXAMPLE_PATH = os.path.join(REPO_ROOT, "config.example.json")
DATA_DIR = os.path.join(REPO_ROOT, "data")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
SECRETS_PATH = os.path.join(REPO_ROOT, "secrets", "secrets.json")

# Secret settings: resolved from env vars / secrets/secrets.json, NEVER written
# back to config.json and NEVER committed (secrets/ is gitignored). This keeps
# API keys out of the (public) repo entirely.
SECRET_KEYS = {
    # config key : environment-variable override
    "tmdb_api_key": "DRIVECAST_TMDB_API_KEY",
}

DEFAULTS = {
    "remote": "gdrive1",
    "tmdb_api_key": "",
    "player": "auto",
    "port": 8737,
    "page_size": 200,
    # Library upgrade:
    "selected_drives": [],            # Shared Drive ids to include (empty = none yet)
    "auto_refresh_on_startup": False,  # rescan the library each launch
    "scan_throttle": 0.15,            # seconds to pause between scan API calls
}

# Keys we persist back to config.json. Secret keys are deliberately excluded so
# they never get written into the repo — they live only in secrets/ or env.
SAVED_KEYS = [k for k in DEFAULTS.keys() if k not in SECRET_KEYS]


def _load_secrets():
    """Read secrets/secrets.json if present. Returns {} on any error/absence."""
    try:
        with open(SECRETS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_secrets(merged):
    """Fill secret keys from (1) env var, (2) secrets/secrets.json, (3) whatever
    was already in config.json (legacy). Precedence: env > secrets file > config."""
    secrets = _load_secrets()
    for key, env_var in SECRET_KEYS.items():
        value = os.environ.get(env_var) or secrets.get(key) or merged.get(key, "")
        merged[key] = (value or "").strip() if isinstance(value, str) else value
    return merged


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
    _resolve_secrets(merged)
    return merged


def save_config(cfg):
    """Persist the known config keys to config.json (atomic write)."""
    out = {}
    for k in SAVED_KEYS:
        if k in cfg:
            out[k] = cfg[k]
    try:
        os.makedirs(REPO_ROOT, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=REPO_ROOT, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass
