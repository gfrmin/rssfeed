"""Per-domain rate limiting + the browser-render fallback decision.

These guard that article fetching stays polite (spaced per domain) and that the
expensive browser tier only fires when the cheap fetch came back empty on a
domain worth rendering (a known SPA paywall, or one we hold a session for).
"""
import asyncio
import time

import httpx

from app import browser_login, extractor


def _run(coro):
    return asyncio.run(coro)

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
    for marker in ("_cf_chl_opt", 'id="challenge-form"'):
        assert browser_login.is_interstitial(f"<html>{marker}</html>") is True


def test_real_article_is_not_an_interstitial():
    assert browser_login.is_interstitial("<article><p>Real prose.</p></article>") is False


def test_js_detections_beacon_is_not_a_challenge():
    """The one marker we must not match.

    Cloudflare serves its JavaScript-detections beacon from the challenge-platform
    path into ordinary 200s on any bot-management zone, and it is still on the page
    you land on after solving a challenge. Treating it as a challenge would discard
    successful renders on exactly the sites the browser tier exists for.
    """
    # A public Cloudflare asset path, not a filesystem path.
    html = ('<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">'  # PII-OK
            '</script><article><p>Real prose.</p></article></html>')
    assert browser_login.is_interstitial(html) is False


# --- the fetch ladder: what "blocked" means, and how it survives ------------

_URL = "https://example.com/a"
_CHALLENGE = "<html><head><script>window._cf_chl_opt={};</script></head></html>"
_ARTICLE = "<html><article><p>Real prose.</p></article></html>"


def _ladder(monkeypatch, replies):
    """Drive ``_fetch_html`` with a scripted reply per tier.

    ``replies`` maps a URL substring to either a body (str) or an HTTP status
    (int) to raise. Wayback is keyed first because its URL embeds the origin's.
    """
    async def _fake_get_via(kwargs, url, proxy, cookies):
        reply = next((r for needle, r in replies.items() if needle in url), None)
        if reply is None:
            raise AssertionError(f"unscripted fetch: {url}")
        if isinstance(reply, int):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                str(reply), request=request,
                response=httpx.Response(reply, request=request),
            )
        return reply

    monkeypatch.setattr(extractor, "_get_via", _fake_get_via)
    monkeypatch.setattr(extractor, "BRIGHTDATA_PROXY", None)
    monkeypatch.setattr(extractor, "BRIGHTDATA_UNLOCKER_PROXY", None)
    return _run(extractor._fetch_html(_URL))


def test_wayback_snapshot_of_a_challenge_is_not_content(monkeypatch):
    """The gap that made the whole escalation a no-op.

    Wayback answers 200 with an archived copy of the origin's *challenge page*.
    That looks like a successful tier, so ``blocked`` used to be dropped and no
    browser render followed — and the challenge stub was stored as the article.
    """
    html, source, blocked = _ladder(
        monkeypatch, {"web.archive.org": _CHALLENGE, "example.com": 403},
    )
    assert (html, source) == (None, None)
    assert blocked is True


def test_blocked_survives_a_successful_wayback(monkeypatch):
    """A real archived article is still content — but the wall is still reported."""
    html, source, blocked = _ladder(
        monkeypatch, {"web.archive.org": _ARTICLE, "example.com": 403},
    )
    assert (html, source, blocked) == (_ARTICLE, "wayback", True)


def test_rate_limiting_does_not_escalate_to_a_browser(monkeypatch):
    """429 is "slow down"; answering it with a full Chromium load is not that."""
    _, _, blocked = _ladder(
        monkeypatch, {"web.archive.org": 500, "example.com": 429},
    )
    assert blocked is False


def test_unauthorized_does_not_escalate_to_a_browser(monkeypatch):
    """401 is a credential problem a cookieless render cannot fix."""
    _, _, blocked = _ladder(
        monkeypatch, {"web.archive.org": 500, "example.com": 401},
    )
    assert blocked is False


def test_forbidden_is_a_wall_worth_a_browser(monkeypatch):
    _, _, blocked = _ladder(
        monkeypatch, {"web.archive.org": 500, "example.com": 403},
    )
    assert blocked is True


# --- _throttle (per-domain spacing) -----------------------------------------

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
