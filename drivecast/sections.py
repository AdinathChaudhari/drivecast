"""Sections: drive -> section assignment, categories, and custom plugins.

A *section* is a top-level area of the app; each selected drive is assigned to
exactly one via the ``drive_sections`` config map (unassigned = entertainment).
Three sections are built in — Entertainment, Courses, Podcasts — and further
**private custom sections** can be dropped into
``~/Library/Application Support/drivecast/sections/`` as plugin ``.py`` files
(same private home as your secrets: never part of the repo). A plugin module
defines a ``SECTION`` dict:

    SECTION = {
        "key": "mysection",          # unique id used in drive_sections
        "label": "My Section",       # tab label
        "icon": "🎧",                # tab emoji
        "accent": "#e08b3c",         # UI accent colour (+ optional "accent2")
        "continue": "Continue Listening",
        "lib": "My Library",         # library heading
        "empty": "No drive yet — assign one in Settings.",
        "season": "Volume",          # season/episode vocabulary
        "episode": "Track",
        "mimes": ("video", "audio", "image"),   # file families to scan
        "classify": my_classifier,   # fn(drive_id, drive_name, nodes, loose)
    }

The classifier is pure and receives the walked node trees exactly like the
built-in classifiers (see courses.py / playlists.py for the record shapes).

A *category* further splits entertainment titles (movie / show / documentary /
other) using the TMDB genre ids we already fetch for posters.
"""
import importlib.util
import logging
import os

from . import config

log = logging.getLogger("drivecast.sections")

BUILTIN_SECTIONS = ("entertainment", "courses", "podcasts")

# Which mime families the scanner walks per built-in section. Entertainment
# stays video-only; courses carry workbook PDFs and cover images; podcasts are
# YouTube downloads (either medium). Plugins declare their own via "mimes".
_BUILTIN_MIMES = {
    "entertainment": ("video",),
    "courses": ("video", "pdf", "image"),
    "podcasts": ("video", "audio"),
}

# Frontend metadata for the built-ins (served via /api/sections so plugins and
# built-ins reach the UI the same way).
_BUILTIN_META = {
    "entertainment": {
        "label": "Entertainment", "icon": "🍿",
        "continue": "Continue Watching", "lib": "Your Library",
        "empty": "No entertainment titles yet — assign drives in Settings and refresh.",
    },
    "courses": {
        "label": "Courses", "icon": "🎓", "accent": "#4ade80", "accent2": "#86efac",
        "continue": "Continue Learning", "lib": "Your Courses",
        "empty": "No course drive yet — assign one to Courses in Settings and it appears here.",
        "season": "Module", "episode": "Lesson",
    },
    "podcasts": {
        "label": "Podcasts", "icon": "🎙", "accent": "#c084fc", "accent2": "#e0b3ff",
        "continue": "Continue Watching", "lib": "Your Podcasts",
        "empty": "No podcast drive yet — assign one in Settings when you've added it.",
    },
}

PLUGIN_DIR = os.path.join(config.USER_DIR, "sections")

TMDB_DOCUMENTARY_GENRE = 99

_plugins = None  # lazy cache: key -> SECTION dict


def _load_plugins():
    """Load custom section plugins from PLUGIN_DIR. Errors never crash the app."""
    found = {}
    if not os.path.isdir(PLUGIN_DIR):
        return found
    for fn in sorted(os.listdir(PLUGIN_DIR)):
        if not fn.endswith(".py") or fn.startswith(("_", "test")):
            continue
        path = os.path.join(PLUGIN_DIR, fn)
        try:
            spec = importlib.util.spec_from_file_location(
                "drivecast_section_%s" % fn[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "SECTION", None)
            key = (meta or {}).get("key")
            if (isinstance(meta, dict) and key and key not in BUILTIN_SECTIONS
                    and callable(meta.get("classify"))):
                found[key] = meta
                log.info("Loaded custom section %r from %s", key, fn)
            else:
                log.warning("Ignoring %s: no valid SECTION dict", fn)
        except Exception:
            log.exception("Failed to load custom section %s", fn)
    return found


def plugins():
    global _plugins
    if _plugins is None:
        _plugins = _load_plugins()
    return _plugins


def all_sections():
    """Every valid section key: built-ins + loaded plugins."""
    return BUILTIN_SECTIONS + tuple(plugins())


def section_for_drive(drive_sections, drive_id):
    """Section for a drive id; unknown/invalid assignments -> entertainment."""
    sec = (drive_sections or {}).get(drive_id)
    return sec if sec in all_sections() else "entertainment"


def mimes_for(section):
    """File families the scanner walks for a section."""
    p = plugins().get(section)
    if p is not None:
        return tuple(p.get("mimes") or ("video",))
    return _BUILTIN_MIMES.get(section, ("video",))


def classify_for(section):
    """A plugin section's classifier, or None for built-ins."""
    p = plugins().get(section)
    return p.get("classify") if p else None


def meta_list():
    """Section metadata for the frontend (/api/sections): built-ins + plugins,
    classifier callables stripped."""
    out = []
    for key in BUILTIN_SECTIONS:
        m = dict(_BUILTIN_META[key])
        m["key"] = key
        out.append(m)
    for key, p in plugins().items():
        m = {k: v for k, v in p.items() if k != "classify" and not callable(v)}
        m["key"] = key
        out.append(m)
    return out


def category_for(meta, structural_type, hint_category=None):
    """Entertainment category from a TMDB enrich() result.

    meta is the cached TMDB dict (or None for a negative lookup):
      * None       -> the hint category if configured, else the structural type
                      ("show" for shows) so a structurally-detected show never
                      falls back to "other"; non-shows fall back to "other"
      * genre 99   -> "documentary" (movies and TV alike)
      * otherwise  -> the structural type ("show" for shows, "movie" else)
    Callers skip this entirely when TMDB is disabled (category stays None and
    the UI falls back to the structural type).
    """
    if meta is None:
        return hint_category or ("show" if structural_type == "show" else "other")
    if TMDB_DOCUMENTARY_GENRE in (meta.get("genre_ids") or []):
        return "documentary"
    return "show" if structural_type == "show" else "movie"
