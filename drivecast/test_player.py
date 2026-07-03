"""Tests for player argument construction (no process is launched)."""
import threading

from drivecast import player
from drivecast.player import PlayerManager, should_advance


def test_mpv_args_include_cache_and_hwdec_flags():
    args = player.build_mpv_args("/usr/bin/mpv", "/tmp/x.sock", 42, "My Movie",
                                 "http://127.0.0.1:8737/stream/abc")
    # Existing behaviour preserved.
    assert args[0] == "/usr/bin/mpv"
    assert "--input-ipc-server=/tmp/x.sock" in args
    assert "--start=42" in args
    assert "--force-media-title=My Movie" in args
    assert "--no-terminal" in args
    assert args[-1] == "http://127.0.0.1:8737/stream/abc"
    # New network-buffering + hw-decode flags.
    for flag in ("--cache=yes", "--cache-secs=30", "--demuxer-max-bytes=150MiB",
                 "--demuxer-max-back-bytes=50MiB", "--demuxer-readahead-secs=20",
                 "--hwdec=auto-safe", "--force-seekable=yes", "--network-timeout=30"):
        assert flag in args


def test_iina_args_use_mpv_prefixed_flags():
    args = player.build_iina_args("/x/iina-cli", "/tmp/y.sock", 0, "Ep",
                                  "http://127.0.0.1:8737/stream/xyz")
    assert "--mpv-input-ipc-server=/tmp/y.sock" in args
    for flag in ("--mpv-cache=yes", "--mpv-hwdec=auto-safe",
                 "--mpv-demuxer-max-bytes=150MiB", "--mpv-network-timeout=30"):
        assert flag in args


def test_vlc_args_enable_http_interface_and_resume():
    args = player.build_vlc_args("/Applications/VLC.app/Contents/MacOS/VLC",
                                 90, "My Show", "http://127.0.0.1:8737/stream/xyz",
                                 8738, "secretpw")
    assert args[0].endswith("/VLC")
    assert "--extraintf" in args and "http" in args
    assert "--http-host" in args and "127.0.0.1" in args
    assert "--http-port" in args and "8738" in args
    assert "--http-password" in args and "secretpw" in args
    assert "--start-time=90" in args
    assert "--play-and-exit" in args
    assert args[-1] == "http://127.0.0.1:8737/stream/xyz"


SAMPLE_STATUS_XML = """<?xml version="1.0" encoding="utf-8"?>
<root>
  <fullscreen>false</fullscreen>
  <time>742</time>
  <volume>256</volume>
  <length>3600</length>
  <state>playing</state>
</root>"""


def test_parse_vlc_status_reads_time_and_length():
    assert player.parse_vlc_status(SAMPLE_STATUS_XML) == (742.0, 3600.0)


def test_parse_vlc_status_handles_garbage_and_missing_fields():
    assert player.parse_vlc_status("not xml at all") == (None, None)
    assert player.parse_vlc_status("<root><state>stopped</state></root>") == (None, None)
    assert player.parse_vlc_status("") == (None, None)


# ---- autoplay: finished-vs-quit rule ----

def test_should_advance_true_near_end():
    # 90% or later of a known duration counts as finished.
    assert should_advance(1350.0, 1500.0) is True   # exactly 90%
    assert should_advance(1490.0, 1500.0) is True   # basically at the end


def test_should_advance_true_within_tail_seconds():
    # Within ~90s of the end counts as finished even if under 90% (long movie).
    assert should_advance(7150.0, 7200.0) is True   # 50s from the end


def test_should_advance_false_quit_early():
    assert should_advance(300.0, 1500.0) is False   # 20% in -> quit early
    assert should_advance(0.0, 1500.0) is False


def test_should_advance_false_unknown_duration():
    assert should_advance(1000.0, 0.0) is False
    assert should_advance(1000.0, None) is False
    assert should_advance(None, None) is False


# ---- autoplay: queue-advance decision ----

def _pm(autoplay=True):
    """A PlayerManager with _launch stubbed to record calls (no real player)."""
    pm = PlayerManager({"autoplay_next": autoplay}, history=None, base_url="http://x")
    calls = []
    pm._launch = lambda *a, **k: calls.append(a)
    pm._calls = calls
    return pm


QUEUE = [
    {"file_id": "e2", "name": "E2", "duration_ms": 1000},
    {"file_id": "e3", "name": "E3", "duration_ms": 2000},
]


def test_advance_launches_next_and_chains_remainder():
    pm = _pm(autoplay=True)
    pm._advance("mpv", "/mpv", QUEUE, "drv", "s1", threading.Event())
    assert len(pm._calls) == 1
    kind, path, file_id, name, dur, drive_id, parent_id, queue = pm._calls[0]
    assert (kind, path, file_id, name, dur) == ("mpv", "/mpv", "e2", "E2", 1000)
    assert (drive_id, parent_id) == ("drv", "s1")
    # The remainder of the queue is chained onto the next item.
    assert [x["file_id"] for x in queue] == ["e3"]


def test_advance_empty_queue_stops():
    pm = _pm(autoplay=True)
    pm._advance("mpv", "/mpv", [], "drv", "s1", threading.Event())
    assert pm._calls == []


def test_advance_autoplay_off_does_not_advance():
    pm = _pm(autoplay=False)
    pm._advance("mpv", "/mpv", QUEUE, "drv", "s1", threading.Event())
    assert pm._calls == []


def test_advance_skips_when_session_superseded():
    pm = _pm(autoplay=True)
    stop = threading.Event()
    stop.set()  # a newer play() replaced this session
    pm._advance("mpv", "/mpv", QUEUE, "drv", "s1", stop)
    assert pm._calls == []
