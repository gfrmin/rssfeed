"""Whether the local Chromium the browser tier needs is actually provisioned.

Playwright launches one *exact* browser revision, but the old check globbed for
any ``chromium-*`` directory. A cache holding some other build — easy to get,
since the standalone `playwright` CLI installs its own — passed the check, so
``login_available()`` reported the tier as working and every render died inside
``launch()`` with "Executable doesn't exist". These tests pin the check to the
revision this playwright actually pins.
"""
import os

from app import browser_login


def _cache(tmp_path, *dirs):
    """Build a fake browser cache holding exactly ``dirs``, and point at it."""
    for d in dirs:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "chrome").write_text("")
    return str(tmp_path)


def _with_cache(monkeypatch, path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", path)
    return browser_login._local_chromium_present()


def test_pinned_revision_is_readable():
    assert (browser_login._pinned_chromium_revision() or "").isdigit()


def test_present_when_the_pinned_revision_is_installed(tmp_path, monkeypatch):
    rev = browser_login._pinned_chromium_revision()
    path = _cache(tmp_path, f"chromium-{rev}/chrome-linux64")
    assert _with_cache(monkeypatch, path) is True


def test_official_build_layout_also_counts(tmp_path, monkeypatch):
    rev = browser_login._pinned_chromium_revision()
    path = _cache(tmp_path, f"chromium-{rev}/chrome-linux")
    assert _with_cache(monkeypatch, path) is True


def test_absent_when_only_another_revision_is_installed(tmp_path, monkeypatch):
    rev = int(browser_login._pinned_chromium_revision())
    path = _cache(tmp_path, f"chromium-{rev + 11}/chrome-linux64")
    assert _with_cache(monkeypatch, path) is False


def test_absent_when_cache_is_empty(tmp_path, monkeypatch):
    assert _with_cache(monkeypatch, str(tmp_path)) is False


def test_missing_registry_reports_absent(monkeypatch):
    monkeypatch.setattr(browser_login, "_pinned_chromium_revision", lambda: None)
    assert browser_login._local_chromium_present() is False


def test_browsers_path_env_is_honoured(tmp_path, monkeypatch):
    """The default is ~/.cache/ms-playwright, but playwright lets it be moved."""
    rev = browser_login._pinned_chromium_revision()
    path = _cache(tmp_path, f"chromium-{rev}/chrome-linux64")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: path)
    assert browser_login._local_chromium_present() is True
