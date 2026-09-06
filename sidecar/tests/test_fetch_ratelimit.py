"""Per-domain rate limiting + the browser-render fallback decision.

These guard that article fetching stays polite (spaced per domain) and that the
expensive browser tier only fires when the cheap fetch came back empty on a
domain worth rendering (a known SPA paywall, or one we hold a session for).
"""
import asyncio
import time

from app import browser_login, extractor

# --- _needs_browser_render (gating the expensive tier) ----------------------

_PAYWALL = "paywall.example.com"     # stands in for a configured paywall site
_UNKNOWN = "example.com"             # no recipe, no session


def _force_browser(monkeypatch, available=True):
    """Pretend chromium is provisioned, and give the run one known-paywall domain.

    The recipe is injected rather than relied on: recipes are operator config now,
    so a test environment has none. A test that leaned on a shipped recipe would
    quietly assert nothing once the list is empty.
    """
    monkeypatch.setattr(extractor.browser_login, "login_available", lambda: available)
    monkeypatch.setattr(
        browser_login,
        "LOGIN_RECIPES",
        {_PAYWALL: browser_login.LoginRecipe(login_url=f"https://{_PAYWALL}/login")},
    )


def test_render_skipped_when_text_is_long(monkeypatch):
    _force_browser(monkeypatch)
    res = {"content_text": "x" * 500}
    assert extractor._needs_browser_render(_PAYWALL, res, None) is False


def test_render_for_short_text_on_known_spa(monkeypatch):
    _force_browser(monkeypatch)
    res = {"content_text": "tiny"}
    assert extractor._needs_browser_render(_PAYWALL, res, None) is True


def test_render_for_short_text_when_we_have_cookies(monkeypatch):
    _force_browser(monkeypatch)
    assert extractor._needs_browser_render(_UNKNOWN, None, {"sess": "1"}) is True


def test_no_render_for_unknown_domain_without_session(monkeypatch):
    _force_browser(monkeypatch)
    assert extractor._needs_browser_render(_UNKNOWN, {"content_text": ""}, None) is False


def test_no_render_when_browser_unavailable(monkeypatch):
    _force_browser(monkeypatch, available=False)
    assert extractor._needs_browser_render(_PAYWALL, None, {"a": "b"}) is False


def test_render_for_unknown_domain_when_every_tier_was_blocked(monkeypatch):
    """An anti-bot wall is the one case where an unknown domain earns a render.

    Cloudflare's "managed" challenge wants JavaScript, so no header, proxy or exit
    IP gets past it and the URL looks dead to every HTTP tier. A real browser
    solves it for free — but only if the gate lets it try.
    """
    _force_browser(monkeypatch)
    assert extractor._needs_browser_render(_UNKNOWN, None, None, blocked=True) is True


def test_blocked_does_not_override_a_good_extraction(monkeypatch):
    _force_browser(monkeypatch)
    res = {"content_text": "x" * 500}
    assert extractor._needs_browser_render(_UNKNOWN, res, None, blocked=True) is False


def test_blocked_still_needs_a_browser(monkeypatch):
    _force_browser(monkeypatch, available=False)
    assert extractor._needs_browser_render(_UNKNOWN, None, None, blocked=True) is False


# --- interstitial detection --------------------------------------------------

def test_unsolved_challenge_is_recognised():
    """Storing a challenge page as article text is worse than storing nothing."""
    for marker in ("_cf_chl_opt", "cdn-cgi/challenge-platform", 'id="challenge-form"'):
        assert browser_login._is_interstitial(f"<html>{marker}</html>") is True


def test_real_article_is_not_an_interstitial():
    assert browser_login._is_interstitial("<article><p>Real prose.</p></article>") is False


# --- _throttle (per-domain spacing) -----------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_throttle_spaces_consecutive_same_domain():
    extractor._domain_last.clear(); extractor._domain_locks.clear()

    async def run():
        t0 = time.monotonic()
        await extractor._throttle("ex.com", 0.4)   # first call: no wait
        await extractor._throttle("ex.com", 0.4)   # second: must wait ~0.4s
        return time.monotonic() - t0

    assert _run(run()) >= 0.4


def test_throttle_does_not_delay_distinct_domains():
    extractor._domain_last.clear(); extractor._domain_locks.clear()

    async def run():
        t0 = time.monotonic()
        await extractor._throttle("a.com", 0.5)
        await extractor._throttle("b.com", 0.5)
        return time.monotonic() - t0

    assert _run(run()) < 0.3


def test_throttle_zero_interval_is_noop():
    async def run():
        t0 = time.monotonic()
        await extractor._throttle("a.com", 0)
        await extractor._throttle(None, 5)
        return time.monotonic() - t0

    assert _run(run()) < 0.1
