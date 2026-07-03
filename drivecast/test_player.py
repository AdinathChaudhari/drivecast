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
