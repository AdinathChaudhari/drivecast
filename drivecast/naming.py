"""Pure filename-parsing helpers.

Turn messy release-style filenames into a clean title, year, and TV episode
info suitable for display and TMDB lookup. No I/O — all pure functions.
"""
import os
import re

# Tokens that mark the end of the "title" part of a release name. Everything
# from the first such token onward is quality/release metadata, not the title.
QUALITY_TOKENS = [
    "2160p", "1080p", "720p", "480p", "4k", "8k",
    "x264", "x265", "h264", "h265", "hevc", "av1", "xvid", "divx",
    "web-dl", "webdl", "webrip", "web", "bluray", "blu-ray", "bdrip", "brrip",
    "dvdrip", "dvd", "hdrip", "hdtv", "remux", "hdr", "hdr10", "dv", "dolby",
    "atmos", "ddp", "dd5", "aac", "ac3", "dts", "truehd", "flac", "mp3",
    "proper", "repack", "extended", "unrated", "remastered", "imax",
    "10bit", "8bit",
]
# Build a regex alternation, longest first so "web-dl" wins over "web".
_QUALITY_SORTED = sorted(QUALITY_TOKENS, key=len, reverse=True)
_QUALITY_ALT = "|".join(re.escape(t) for t in _QUALITY_SORTED)
_QUALITY_RE = re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % _QUALITY_ALT, re.IGNORECASE)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SXXEXX_RE = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", re.IGNORECASE)
_NXNN_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"[\[\(\{].*?[\]\)\}]")
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts"}


def strip_ext(name):
    """Remove a trailing video file extension (only known video extensions)."""
    root, ext = os.path.splitext(name)
    if ext.lower() in _VIDEO_EXTS:
        return root
    return name


def _normalize_separators(text):
    """Turn dots, underscores and multiple spaces into single spaces."""
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def detect_episode(name):
    """Return (season, episode) as ints if this looks like TV, else None."""
    m = _SXXEXX_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _NXNN_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def extract_year(name):
    """Return a 4-digit year (1900-2099) as int, else None.

    When several year-like tokens are present (e.g. "Blade Runner 2049 2017"),
    the last one is almost always the release year, so we return that.
    """
    matches = _YEAR_RE.findall(name)
    if not matches:
        return None
    # findall returns the group ("19"/"20"); re-scan for full matches instead.
    full = [m.group(0) for m in _YEAR_RE.finditer(name)]
    return int(full[-1])


def _title_case(text):
    """Title-case a cleaned title, leaving obvious acronyms alone-ish."""
    words = text.split()
    out = []
    for w in words:
        if w.isupper() and len(w) <= 4:
            out.append(w)  # keep short all-caps (acronyms)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(out)


def parse(name):
    """Parse a filename into a display dict.

    Returns: {
      "title": clean title,
      "year": int or None,
      "type": "movie" | "tv",
      "season": int or None,
      "episode": int or None,
      "raw": original name,
    }
    """
    raw = name
    base = strip_ext(name)
    # Normalise separators first so word boundaries work even when the original
    # used dots/underscores (e.g. "Game_of_Thrones_S01E01"). Brackets are kept
    # for now and removed from the title region below.
    base = re.sub(r"[._]+", " ", base)

    episode = detect_episode(base)
    is_tv = episode is not None

    # Cut the title at the episode marker (for TV) so "Show S01E02 rest" -> "Show".
    title_region = base
    if is_tv:
        m = _SXXEXX_RE.search(base) or _NXNN_RE.search(base)
        if m:
            title_region = base[: m.start()]

    # Remove bracketed groups ([YTS.AM], (2016) handled separately, etc.).
    # Extract year before stripping brackets, from the whole base.
    year = extract_year(base)

    title_region = _BRACKETS_RE.sub(" ", title_region)

    # Truncate at the first quality/release token.
    qm = _QUALITY_RE.search(title_region)
    if qm:
        title_region = title_region[: qm.start()]

    title_region = _normalize_separators(title_region)

    # Drop a trailing year token from the title itself.
    if year is not None:
        title_region = re.sub(r"[\s\-]*\(?%d\)?\s*$" % year, "", title_region).strip()
        # also remove year anywhere as a standalone token near the end
        title_region = re.sub(r"\b%d\b" % year, "", title_region).strip()

    # Strip trailing separators / dashes left over.
    title_region = title_region.strip(" -_.")
    title_region = _normalize_separators(title_region)

    title = _title_case(title_region) if title_region else strip_ext(raw)

    result = {
        "title": title,
        "year": year,
        "type": "tv" if is_tv else "movie",
        "season": episode[0] if episode else None,
        "episode": episode[1] if episode else None,
        "raw": raw,
    }
    return result
