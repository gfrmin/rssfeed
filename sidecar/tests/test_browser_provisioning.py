"""Whether the browser the render tier actually launches is provisioned.

Two separate false answers have lived in ``_local_chromium_present``, and both
were silent. Globbing any ``chromium-*`` directory accepted a cache holding some
*other* revision, so ``login_available()`` reported the tier as working and every
render died inside ``launch()`` with "Executable doesn't exist". Pinning the
revision but globbing ``chromium-<rev>/.../chrome`` then checked a binary that is
never executed: this module only ever launches headless, and playwright runs a
separate ``chrome-headless-shell`` for that — so a headless-shell-only cache (a
documented, much smaller install) reported the tier as missing while it worked.

These tests pin the check to the binary a headless ``launch()`` really runs.
"""
import sys

import pytest

from app import browser_login

# Relative cache layouts, built under pytest's tmp_path. No real filesystem root
# appears here or anywhere else in this file.
_SHELL = "chromium_headless_shell-{rev}/chrome-headless-shell-linux64/chrome-headless-shell"  # PII-OK
_SHELL_OLD = "chromium_headless_shell-{rev}/chrome-linux/headless_shell"  # PII-OK
_FULL = "chromium-{rev}/chrome-linux64/chrome"  # PII-OK


@pytest.fixture
def revisions():
    return browser_login._pinned_revisions()


def _cache(tmp_path, *binaries):
    """Materialise a fake browser cache containing exactly ``binaries``."""
    for rel in binaries:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    return str(tmp_path)


def _present_with(monkeypatch, path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", path)
    return browser_login._local_chromium_present()


# --- the registry ------------------------------------------------------------

def test_registry_pins_a_headless_shell_revision(revisions):
    """The headless shell is what we launch, so it must be in the registry."""
    assert revisions["chromium-headless-shell"].isdigit()


# --- what counts as provisioned ---------------------------------------------

def test_present_when_the_headless_shell_is_installed(tmp_path, monkeypatch, revisions):
    rev = revisions["chromium-headless-shell"]
    path = _cache(tmp_path, _SHELL.format(rev=rev))
    assert _present_with(monkeypatch, path) is True


def test_older_headless_shell_layout_also_counts(tmp_path, monkeypatch, revisions):
    rev = revisions["chromium-headless-shell"]
    path = _cache(tmp_path, _SHELL_OLD.format(rev=rev))
    assert _present_with(monkeypatch, path) is True


def test_absent_when_only_the_headed_build_is_installed(tmp_path, monkeypatch, revisions):
    """The regression that motivated this file.

    ``launch(headless=True)`` never runs ``chromium-<rev>/chrome-linux64/chrome``
    — verified against the installed playwright by reading ``/proc/<pid>/exe`` of
    a live launch. A cache holding only that build must report absent, or the
    tier claims to work and then dies inside ``launch()``.
    """
    path = _cache(tmp_path, _FULL.format(rev=revisions["chromium-headless-shell"]))
    assert _present_with(monkeypatch, path) is False


def test_absent_when_only_another_revision_is_installed(tmp_path, monkeypatch, revisions):
    rev = int(revisions["chromium-headless-shell"]) + 11
    path = _cache(tmp_path, _SHELL.format(rev=rev))
    assert _present_with(monkeypatch, path) is False


def test_absent_when_cache_is_empty(tmp_path, monkeypatch):
    assert _present_with(monkeypatch, str(tmp_path)) is False


def test_headed_build_is_the_fallback_for_pre_shell_playwrights(tmp_path, monkeypatch):
    """Playwright <1.49 shipped no separate shell, so chromium was the binary."""
    monkeypatch.setattr(browser_login, "_pinned_revisions", lambda: {"chromium": "999"})
    path = _cache(tmp_path, _FULL.format(rev="999"))
    assert _present_with(monkeypatch, path) is True


# --- locating the cache ------------------------------------------------------

def test_browsers_path_env_is_honoured(tmp_path, monkeypatch, revisions):
    rev = revisions["chromium-headless-shell"]
    path = _cache(tmp_path, _SHELL.format(rev=rev))
    assert _present_with(monkeypatch, path) is True


def test_xdg_cache_home_is_honoured(tmp_path, monkeypatch, revisions):
    """Playwright resolves its default cache under XDG_CACHE_HOME when that is set,
    rather than assuming a cache directory beneath the home directory.
    """
    rev = revisions["chromium-headless-shell"]
    _cache(tmp_path / "ms-playwright", _SHELL.format(rev=rev))  # PII-OK
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert browser_login._local_chromium_present() is True


def test_browsers_path_zero_is_a_mode_not_a_path(monkeypatch):
    """``PLAYWRIGHT_BROWSERS_PATH=0`` means "install beside the package".

    Read as a path it becomes a relative directory named ``0``, which matches
    nothing — silently disabling the browser tier on a machine where the
    browsers are installed exactly as playwright put them.
    """
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    base = browser_login._browsers_base()
    assert base is not None
    assert base.endswith(".local-browsers")


# --- fail-open ---------------------------------------------------------------

def test_missing_registry_reports_absent(monkeypatch):
    monkeypatch.setattr(browser_login, "_playwright_registry", lambda: None)
    assert browser_login._local_chromium_present() is False


def test_absent_playwright_degrades_instead_of_raising(monkeypatch):
    """The browser tier is optional; a broken wheel must not take the app down.

    ``_local_chromium_present()`` runs at import of ``app.browser_login``, which
    ``app.extractor`` and the entry routes import in turn — so an unguarded
    ``import playwright`` here is an app-wide import failure, not a degraded tier.
    """
    monkeypatch.setitem(sys.modules, "playwright", None)
    assert browser_login._playwright_package_root() is None
    assert browser_login._playwright_registry() is None
    assert browser_login._local_chromium_present() is False
