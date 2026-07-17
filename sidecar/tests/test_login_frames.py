"""Which frames may receive the user's subscription credentials.

The heuristic login path matches generic selectors (`input[type=email]`,
`input[name*=user i]`) against frames. Scanning every frame means a third-party
iframe that merely happens to contain matching inputs — a newsletter widget, an
ad unit — can win that race and be handed real credentials, which the code then
submits to it. These tests pin down that only trusted frames are ever offered.
"""
import asyncio

from app import browser_login


def run(coro):
    return asyncio.run(coro)


class _Frame:
    def __init__(self, url):
        self.url = url


class _Page:
    def __init__(self, main_url, frame_urls):
        self.main_frame = _Frame(main_url)
        # Playwright includes the main frame in page.frames.
        self.frames = [self.main_frame] + [_Frame(u) for u in frame_urls]


def _hosts(frames):
    return [browser_login._frame_host(f.url) for f in frames]


def test_main_frame_is_always_trusted():
    page = _Page("https://example.com/login", [])
    assert _hosts(browser_login.trusted_login_frames(page, "https://example.com/login")) == [
        "example.com"
    ]


def test_untrusted_third_party_frame_is_excluded():
    """The actual leak scenario: an ad/newsletter iframe with matching inputs."""
    page = _Page("https://example.com/login", ["https://ads.doubleclick.net/x"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://example.com/login"))
    assert "ads.doubleclick.net" not in got
    assert got == ["example.com"]


def test_same_site_subdomain_frame_is_trusted():
    page = _Page("https://example.com/login", ["https://accounts.example.com/form"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://example.com/login"))
    assert "accounts.example.com" in got


def test_known_auth_provider_frame_is_trusted():
    """Piano/tinypass host the real login form, so refusing all cross-origin
    frames would break the sites this feature exists for."""
    page = _Page("https://nationalreview.com/login", ["https://buy.tinypass.com/checkout"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://nationalreview.com/login"))
    assert "buy.tinypass.com" in got


def test_lookalike_domain_is_not_trusted():
    """endswith() on a bare name would trust `evil-example.com`; require a dot."""
    page = _Page("https://example.com/login", ["https://evil-example.com/x"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://example.com/login"))
    assert "evil-example.com" not in got


def test_auth_provider_lookalike_is_not_trusted():
    page = _Page("https://example.com/login", ["https://nottinypass.com/x"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://example.com/login"))
    assert "nottinypass.com" not in got


def test_about_blank_and_unparseable_frames_are_skipped():
    page = _Page("https://example.com/login", ["about:blank", ""])
    got = browser_login.trusted_login_frames(page, "https://example.com/login")
    assert _hosts(got) == ["example.com"]


def test_same_site_matching_is_case_insensitive():
    page = _Page("https://Example.com/login", ["https://Accounts.EXAMPLE.com/f"])
    got = _hosts(browser_login.trusted_login_frames(page, "https://Example.com/login"))
    assert "accounts.example.com" in got


# --- the trust check must be re-taken, not remembered -----------------------
# Reported by review with working PoCs: trust was decided once when frames were
# listed, but the two-step path clicks a button and waits 2.5s before typing the
# password. If that click navigates the frame off-site — an SSO bounce, an open
# redirect, an auth-provider hop — the password went to wherever it landed. The
# main frame was worse: trusted unconditionally, so it stayed "trusted" after
# navigating anywhere at all.

class _Loc:
    """A stand-in for a Playwright locator that records what was typed."""
    def __init__(self, frame, kind):
        self.frame, self.kind, self.filled = frame, kind, None

    async def fill(self, value):
        self.filled = value
        self.frame.typed.append((self.kind, value))

    async def click(self):
        await self.frame.on_click()

    async def press(self, _key):
        pass


class _NavFrame:
    """A frame whose URL changes when its Continue button is clicked."""
    def __init__(self, url, nav_to=None, has_password_before_click=False):
        self.url, self._nav_to = url, nav_to
        self.has_password = has_password_before_click
        self.typed = []

    async def on_click(self):
        if self._nav_to:
            self.url = self._nav_to        # the navigation
            self.has_password = True       # the "password step" now renders


class _NavPage:
    def __init__(self, main, others=()):
        self.main_frame = main
        self.frames = [main, *others]

    async def wait_for_timeout(self, _ms):
        pass


def _install(monkeypatch, page):
    """Route _first_visible at our fake frames."""
    async def first_visible(scope, selectors):
        frame = scope.main_frame if isinstance(scope, _NavPage) else scope
        sel = selectors[0]
        if "pass" in sel or "password" in sel:
            return _Loc(frame, "password") if frame.has_password else None
        if "submit" in sel or "button" in sel:
            return _Loc(frame, "submit")
        return _Loc(frame, "username")
    monkeypatch.setattr(browser_login, "_first_visible", first_visible)


def _recipe():
    return browser_login.LoginRecipe(
        login_url="https://example.com/login",
        username_selectors=["#user"], password_selectors=["#password"],
        submit_selectors=["#submit"],
    )


def test_password_not_typed_after_main_frame_navigates_off_site(monkeypatch):
    """PoC variant B: no iframe needed. The top-level login page's Continue button
    submits to another origin; the main frame used to keep its free pass."""
    main = _NavFrame("https://example.com/login", nav_to="https://attacker.example.net/x")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "user@example.com", "SECRET"))
    assert ok is False
    assert not any(kind == "password" for kind, _ in main.typed), main.typed


def test_password_not_typed_after_trusted_iframe_navigates_off_site(monkeypatch):
    """PoC variant A: a same-site iframe passes the initial check, then its
    Continue button lands it on an untrusted origin."""
    main = _NavFrame("https://example.com/login")
    inner = _NavFrame("https://example.com/form", nav_to="https://attacker.example.net/x")
    page = _NavPage(main, [inner])
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "user@example.com", "SECRET"))
    assert not any(kind == "password" for kind, _ in inner.typed), inner.typed
    assert not any(kind == "password" for kind, _ in main.typed), main.typed
    assert ok is False


def test_two_step_still_works_when_it_stays_on_site(monkeypatch):
    """The guard must not break the legitimate flow it's protecting: a same-site
    two-step login should still complete."""
    main = _NavFrame("https://example.com/login", nav_to="https://example.com/login?step=2")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "user@example.com", "SECRET"))
    assert ok is True
    assert ("password", "SECRET") in main.typed


def test_two_step_still_works_via_allowlisted_auth_provider(monkeypatch):
    """A bounce to Piano/tinypass is the flow this feature exists for."""
    main = _NavFrame("https://example.com/login", nav_to="https://buy.tinypass.com/checkout")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "user@example.com", "SECRET"))
    assert ok is True
    assert ("password", "SECRET") in main.typed
