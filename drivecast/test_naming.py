"""Tests for naming.parse and helpers."""
import pytest

from drivecast import naming


@pytest.mark.parametrize("name,title,year,typ", [
    ("Your.Name.2016.1080p.BluRay.x264-[YTS.AM].mp4", "Your Name", 2016, "movie"),
    ("The.Matrix.1999.2160p.UHD.BluRay.x265-TERMINAL.mkv", "The Matrix", 1999, "movie"),
    ("Inception (2010) [1080p] [BluRay].mp4", "Inception", 2010, "movie"),
    ("Blade Runner 2049 2017 REMUX 2160p HDR.mkv", "Blade Runner 2049", 2017, "movie"),
    ("Interstellar.2014.WEB-DL.DDP5.1.Atmos.mkv", "Interstellar", 2014, "movie"),
    ("Spirited_Away_2001_1080p_BDRip.mp4", "Spirited Away", 2001, "movie"),
    ("Dune.Part.Two.2024.WEBRip.x265.HEVC.mkv", "Dune Part Two", 2024, "movie"),
    ("Parasite 2019 720p PROPER.mkv", "Parasite", 2019, "movie"),
])
def test_movies(name, title, year, typ):
    r = naming.parse(name)
    assert r["title"] == title
    assert r["year"] == year
    assert r["type"] == typ


@pytest.mark.parametrize("name,title,season,episode", [
    ("Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.mkv", "Breaking Bad", 5, 14),
    ("Frasier (1993) - S05E10 - Where Every Bloke.mkv", "Frasier", 5, 10),
    ("The Office 3x06 720p.mkv", "The Office", 3, 6),
    ("Game_of_Thrones_S01E01_x264.mp4", "Game Of Thrones", 1, 1),
    ("Attack on Titan S04E28 HEVC 1080p.mkv", "Attack On Titan", 4, 28),
])
def test_tv(name, title, season, episode):
    r = naming.parse(name)
    assert r["type"] == "tv"
    assert r["title"] == title
    assert r["season"] == season
    assert r["episode"] == episode


def test_no_year_no_quality():
    r = naming.parse("Random Home Video.mp4")
    assert r["title"] == "Random Home Video"
    assert r["year"] is None
    assert r["type"] == "movie"


def test_strip_ext_only_known():
    assert naming.strip_ext("Movie.Name.mp4") == "Movie.Name"
    assert naming.strip_ext("Movie.Name.2020") == "Movie.Name.2020"  # .2020 not a video ext


def test_extract_year_bounds():
    assert naming.extract_year("Film 1899.mkv") is None  # 18xx out of range
    assert naming.extract_year("Film 2099.mkv") == 2099


def test_detect_episode_variants():
    assert naming.detect_episode("Show.S02E05") == (2, 5)
    assert naming.detect_episode("Show 2x05") == (2, 5)
    assert naming.detect_episode("Show 2020") is None


@pytest.mark.parametrize("folder,season", [
    ("Season 1", 1),
    ("Season 01", 1),
    ("Series 2", 2),
    ("S01", 1),
    ("S3", 3),
    ("Specials", 0),
    ("Extras", None),
    ("Random Folder", None),
])
def test_season_from_folder(folder, season):
    assert naming.season_from_folder(folder) == season


@pytest.mark.parametrize("name,ep", [
    ("Show.S01E07.mkv", 7),
    ("Show 2x09.mkv", 9),
    ("Show - Episode 12.mkv", 12),
    ("Show E04 1080p.mkv", 4),
    ("Just A Movie.mkv", None),
])
def test_episode_number(name, ep):
    assert naming.episode_number(name) == ep


@pytest.mark.parametrize("name", [
    "Movie-sample.mkv", "Cool Trailer.mp4", "Some Featurette.mkv", "The Extra.mkv",
])
def test_is_sample(name):
    assert naming.is_sample(name) is True


def test_is_sample_negative():
    assert naming.is_sample("The Matrix 1999.mkv") is False


def test_episode_title():
    assert naming.episode_title("Frasier (1993) - S05E10 - Where Every Bloke.mkv") == "Where Every Bloke"
    assert naming.episode_title("Show.S01E01.1080p.BluRay.mkv") is None  # nothing but quality after marker
    assert naming.episode_title("No Marker Here.mkv") is None


def test_clean_title():
    assert naming.clean_title("Your Name (2016) [BluRay] [1080p]") == ("Your Name", 2016)


def test_pure_season():
    from drivecast import naming
    assert naming.pure_season("Season 1") == 1
    assert naming.pure_season("Season 03") == 3
    assert naming.pure_season("S02") == 2
    assert naming.pure_season("Series 4") == 4
    assert naming.pure_season("Specials") == 0
    assert naming.pure_season("Blackadder Season 1") is None
    assert naming.pure_season("The Office") is None


def test_split_season_suffix():
    from drivecast import naming
    assert naming.split_season_suffix("Blackadder Season 1 S01") == ("Blackadder", 1)
    assert naming.split_season_suffix("Blackadder Season 2") == ("Blackadder", 2)
    assert naming.split_season_suffix("Foo S02") == ("Foo", 2)
    assert naming.split_season_suffix("Blackadder Specials") == ("Blackadder", 0)
    # Range-named whole-series folder is NOT a single season.
    assert naming.split_season_suffix("The Office Season 1-9 S01-s09") == (None, None)
    # A bare season has no show prefix.
    assert naming.split_season_suffix("Season 1") == (None, None)


@pytest.mark.parametrize("name,stripped", [
    ("01) Mission Impossible", "Mission Impossible"),
    ("01.Iron Man (2008) [1080p]", "Iron Man (2008) [1080p]"),
    ("1 - Something", "Something"),
    ("1. The Thing", "The Thing"),
    ("02) The Incredible Hulk (2008)", "The Incredible Hulk (2008)"),
    # Real leading title numbers must NOT be stripped.
    ("2 Fast 2 Furious", "2 Fast 2 Furious"),
    ("300", "300"),
    ("1917", "1917"),
    ("9 (2009)", "9 (2009)"),
])
def test_strip_enum_prefix(name, stripped):
    assert naming.strip_enum_prefix(name) == stripped


@pytest.mark.parametrize("name,title,year", [
    ("01) Mission Impossible (1996) [1080p]", "Mission Impossible", 1996),
    ("01.Iron Man (2008) 1080p BluRay.mkv", "Iron Man", 2008),
    ("02) The Incredible Hulk (2008) [1080p]", "The Incredible Hulk", 2008),
    # Leading real number preserved through the full parse.
    ("2 Fast 2 Furious 2003 1080p.mkv", "2 Fast 2 Furious", 2003),
])
def test_parse_strips_enumeration(name, title, year):
    r = naming.parse(name)
    assert r["title"] == title
    assert r["year"] == year


@pytest.mark.parametrize("folder,is_extras", [
    ("Featurettes", True),
    ("EXTRAS", True),
    ("Behind the Scenes", True),
    ("Deleted Scenes", True),
    ("Samples", True),
    ("Subtitles", True),
    ("Trailers", True),
    ("Iron Man (2008)", False),
    ("Season 1", False),
])
def test_is_extras_folder(folder, is_extras):
    assert naming.is_extras_folder(folder) is is_extras


def test_season_parsers_ignore_quality_noise():
    from drivecast import naming
    # Real-world folder names carry bracketed quality junk.
    assert naming.pure_season("Season 1 (480p DVD)") == 1
    assert naming.split_season_suffix(
        "Blackadder (1983) Season 1 S01 (576p DVD x265 HEVC 10bit AAC 2.0 Panda)"
    ) == ("Blackadder", 1)
    assert naming.split_season_suffix("Blackadder (1983) Specials") == ("Blackadder", 0)
