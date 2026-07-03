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

# Season folder names: "Season 1", "Season 01", "Series 2", "S01", "S1".
_SEASON_WORD_RE = re.compile(r"\b(?:season|series)\s*0*(\d+)\b", re.IGNORECASE)
_SEASON_SHORT_RE = re.compile(r"^\s*s0*(\d+)\s*$", re.IGNORECASE)
# Episode markers beyond SxxExx / NxNN.
_EPISODE_WORD_RE = re.compile(r"\bepisode\s*0*(\d+)\b", re.IGNORECASE)
_EP_SHORT_RE = re.compile(r"\bE0*(\d+)\b", re.IGNORECASE)
# Files that are not the feature/episode itself.
_SAMPLE_RE = re.compile(r"\b(?:sample|trailer|featurette|extra|extras)\b", re.IGNORECASE)

# Subfolder names that hold bonus material, not the feature — skipped when a
# folder is expanded into movies (case-insensitive, exact folder name).
_EXTRAS_FOLDERS = {
    "featurettes", "extras", "bonus", "behind the scenes", "deleted scenes",
    "sample", "samples", "subs", "subtitles", "trailers",
}

# A leading enumeration prefix on a folder/file name, e.g. "01) ", "01.", "1 - ".
# Two safe forms only:
#   * digits + a )./- separator surrounded by spaces:  "01) X", "1 - X", "1. X"
#   * digits + a dot immediately before a letter:       "01.Iron Man"
# This deliberately does NOT match real leading title numbers like "2 Fast 2
# Furious" (a space, not a separator, follows the digit) or "300"/"1917".
_ENUM_PREFIX_RE = re.compile(r"^\s*(?:\d{1,3}\s*[).\-]\s+|\d{1,3}\.(?=[A-Za-z]))")


def is_extras_folder(name):
    """True if a folder name denotes bonus material (featurettes/extras/...)."""
    return (name or "").strip().lower() in _EXTRAS_FOLDERS


def strip_enum_prefix(name):
    """Remove a leading enumeration prefix ("01) ", "01.", "1 - ") from a name."""
    return _ENUM_PREFIX_RE.sub("", name or "", count=1)


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


def is_video_name(name):
    """True if the filename has a known video extension."""
    _, ext = os.path.splitext(name or "")
    return ext.lower() in _VIDEO_EXTS


def is_sample(name):
    """True for sample/trailer/featurette/extra files that aren't the feature."""
    return bool(_SAMPLE_RE.search(name or ""))


def season_from_folder(name):
    """Return a season number from a folder name, else None.

    Matches "Season 1", "Series 2", "S01", "S1", and treats a "Specials"
    folder as season 0.
    """
    if not name:
        return None
    if name.strip().lower() == "specials":
        return 0
    m = _SEASON_WORD_RE.search(name)
    if m:
        return int(m.group(1))
    m = _SEASON_SHORT_RE.match(name)
    if m:
        return int(m.group(1))
    return None


def _strip_folder_noise(name):
    """Remove bracketed groups and quality/release tokens from a folder name.

    Folder names in the wild carry junk like "Season 1 (480p DVD)" or
    "Blackadder (1983) Season 1 S01 (576p DVD x265 ...)". Stripping it first lets
    the season detectors see the real "Season 1" / "Blackadder Season 1 S01".
    """
    t = _BRACKETS_RE.sub(" ", name or "")
    t = _QUALITY_RE.sub(" ", t)
    return _normalize_separators(t)


def pure_season(name):
    """Return a season number if the name is *just* a season marker, else None.

    "Season 3" -> 3, "S03" -> 3, "Series 2" -> 2, "Specials" -> 0, and the same
    with trailing junk like "Season 1 (480p DVD)". Used to detect a drive whose
    top-level folders are bare seasons (the drive itself is the show). A name with
    other real words (e.g. "Blackadder Season 1") is NOT pure.
    """
    if not name:
        return None
    t = _strip_folder_noise(name)
    if t.lower() == "specials":
        return 0
    m = re.fullmatch(r"(?:season|series)\s*0*(\d+)", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"s0*(\d+)", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def split_season_suffix(name):
    """Split "<Show> <season marker>" into (show_prefix, season_number).

    "Blackadder Season 1 S01" -> ("Blackadder", 1); "Foo Season 2" -> ("Foo", 2);
    "Foo S02" -> ("Foo", 2); "Foo Specials" -> ("Foo", 0). Returns (None, None)
    when the name doesn't end in a single-season marker. A numeric RANGE like
    "The Office Season 1-9 S01-s09" is rejected (it's a whole-series folder, not
    one season), so such folders are left as their own record.
    """
    if not name:
        return (None, None)
    t = _strip_folder_noise(name)
    if re.search(r"\d+\s*[-–]\s*\d+", t):  # a range (1-9, S01-S09) -> not one season
        return (None, None)
    # trailing bare "S01" (possibly preceded by a "Season N" word form)
    m = re.search(r"^(.*?)\s+s0*(\d+)$", t, re.IGNORECASE)
    if m:
        prefix, season = m.group(1), int(m.group(2))
        m2 = re.search(r"^(.*?)\s+(?:season|series)\s*0*(\d+)$", prefix, re.IGNORECASE)
        if m2:
            prefix, season = m2.group(1), int(m2.group(2))
        prefix = prefix.strip(" -_.")
        return (prefix, season) if prefix else (None, None)
    # trailing "Season N" / "Series N"
    m = re.search(r"^(.*?)\s+(?:season|series)\s*0*(\d+)$", t, re.IGNORECASE)
    if m:
        prefix = m.group(1).strip(" -_.")
        return (prefix, int(m.group(2))) if prefix else (None, None)
    # trailing "Specials"
    m = re.search(r"^(.*?)\s+specials$", t, re.IGNORECASE)
    if m:
        prefix = m.group(1).strip(" -_.")
        return (prefix, 0) if prefix else (None, None)
    return (None, None)


def episode_number(name):
    """Return an episode number from a filename, else None.

    Recognises SxxExx, NxNN, "Episode 5", and a bare "E05" token.
    """
    ep = detect_episode(name)
    if ep is not None:
        return ep[1]
    m = _EPISODE_WORD_RE.search(name)
    if m:
        return int(m.group(1))
    m = _EP_SHORT_RE.search(name)
    if m:
        return int(m.group(1))
    return None


def clean_title(name):
    """Return (title, year) for a folder or filename via parse()."""
    p = parse(name)
    return p["title"], p["year"]


def episode_title(name):
    """Best-effort episode name (the text after the SxxExx/NxNN marker), else None.

    e.g. "Frasier (1993) - S05E10 - Where Every Bloke.mkv" -> "Where Every Bloke".
    """
    base = strip_ext(name)
    base = re.sub(r"[._]+", " ", base)
    m = _SXXEXX_RE.search(base) or _NXNN_RE.search(base)
    if not m:
        return None
    tail = base[m.end():]
    tail = _BRACKETS_RE.sub(" ", tail)
    qm = _QUALITY_RE.search(tail)
    if qm:
        tail = tail[: qm.start()]
    tail = tail.strip(" -_.")
    tail = _normalize_separators(tail)
    return _title_case(tail) if tail else None


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
    # Drop a leading enumeration prefix ("01) ", "01.Iron Man", "1 - ...") BEFORE
    # separator normalisation, since normalising dots would erase the "01." signal.
    base = strip_enum_prefix(base)
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
