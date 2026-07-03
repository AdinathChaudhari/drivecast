"""Tests for player argument construction (no process is launched)."""
from drivecast import player


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
