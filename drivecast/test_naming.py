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
