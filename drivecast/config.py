"""Configuration: merge config.json over defaults, auto-create on first run.

Config, data and secrets live in a STABLE per-user directory
(~/Library/Application Support/drivecast) rather than inside the repo/app
bundle. This means the packaged .app can read the user's TMDB key and selected
drives, and rebuilding/reinstalling the bundle never wipes them. Only the
first-run example config is read from the repo.
"""
import json
import os
import shutil
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Stable user directory that survives app rebuilds and is writable by the bundle.
USER_DIR = os.path.expanduser("~/Library/Application Support/drivecast")
CONFIG_PATH = os.path.join(USER_DIR, "config.json")
# First-run template still ships in the repo.
EXAMPLE_PATH = os.path.join(REPO_ROOT, "config.example.json")
DATA_DIR = os.path.join(USER_DIR, "data")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
SUBS_DIR = os.path.join(DATA_DIR, "subs")
SECRETS_PATH = os.path.join(USER_DIR, "secrets", "secrets.json")

# Secret settings: resolved from env vars / secrets/secrets.json, NEVER written
# back to config.json and NEVER committed (secrets/ is gitignored). This keeps
# API keys out of the (public) repo entirely.
SECRET_KEYS = {
    # config key : environment-variable override
    "tmdb_api_key": "DRIVECAST_TMDB_API_KEY",
    "opensubtitles_api_key": "DRIVECAST_OPENSUBTITLES_API_KEY",
}

DEFAULTS = {
    "remote": "gdrive1",
    "tmdb_api_key": "",
    "opensubtitles_api_key": "",
    "player": "auto",
    # Load English subtitles when available (sibling .srt on the drive, or
    # OpenSubtitles when an API key is configured in secrets).
    "subtitles": True,
    "port": 8737,
    "https_port": 8738,               # trusted-LAN HTTPS listener (remote access)
    "page_size": 200,
    # Library upgrade:
    "selected_drives": [],            # Shared Drive ids to include (empty = none yet)
    "auto_refresh_on_startup": False,  # rescan the library each launch
    "scan_throttle": 0.15,            # seconds to pause between scan API calls
    "autoplay_next": True,            # auto-play the next episode when one finishes
    # Hold a macOS power assertion (caffeinate) while any stream is active, so a
    # lid-closed/clamshell Mac doesn't sleep and kill remote playback.
    "keep_awake": True,
    # Remote access (opt-in): when enabled the server binds to the LAN/tailnet
    # instead of loopback and every non-local request must carry the secret
    # token. Both keys are non-secret and auto-saved (the token is generated on
    # first enable, so it lives in config.json — treat the link like a password).
    "remote_access": False,
    "remote_token": "",
    # Which app section each drive belongs to: drive_id ->
    # "entertainment" | "courses" | "podcasts" | a custom plugin section
    # (see sections.py). Unassigned drives
    # are entertainment. Set from Settings.
    "drive_sections": {},
    # Per-drive classifier hints (hand-edited; no UI). Shapes:
    #   {"<drive_id>": {"category": "documentary"}}   TMDB-miss category fallback
    #   {"<drive_id>": {"single_course": true}}       whole drive is ONE course
    "drive_hints": {},
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
        os.makedirs(USER_DIR, exist_ok=True)
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
        os.makedirs(USER_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=USER_DIR, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass
