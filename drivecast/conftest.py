"""Shared test fixtures.

Tests must be hermetic: custom section plugins installed in the developer's
user directory must never leak into test runs, so the plugin dir is pointed
at an empty temp path for every test (tests that exercise plugin loading
override this themselves).
"""
import pytest

from drivecast import sections


@pytest.fixture(autouse=True)
def _no_real_plugins(monkeypatch, tmp_path):
    monkeypatch.setattr(sections, "PLUGIN_DIR", str(tmp_path / "_no_plugins"))
    monkeypatch.setattr(sections, "_plugins", None)
    yield
