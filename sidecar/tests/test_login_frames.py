"""Which frames may receive the user's subscription credentials.

The heuristic login path matches generic selectors (`input[type=email]`,
`input[name*=user i]`) against frames. Scanning every frame means a third-party
iframe that merely happens to contain matching inputs — a newsletter widget, an
ad unit — can win that race and be handed real credentials, which the code then
submits to it. These tests pin down that only trusted frames are ever offered.
"""
import asyncio

from app import browser_login, egress


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
        self.frame.typed.append(("clicked", self.kind))
        await self.frame.on_click()

    async def press(self, _key):
        self.frame.typed.append(("submitted", None))

    async def evaluate(self, _js):
        # Models the RAW DOM facts _SUBMIT_DEST_JS reads — accurately, including the
        # trap that sank the first version: `el.formAction` is NEVER empty for a
        # plain button, it reflects the document URL when there's no `formaction`
        # attribute. `form.action` is where the form really posts (its own attr, or
        # the doc URL when unset). A mock that returned the "intended" destination
        # instead of these raw facts is exactly what hid the original bug.
        return {
            "formactionAttr": self.frame.formaction_attr,          # usually None
            "formAction": self.frame.formaction_attr or self.frame.url,
            "formAction_of_form": self.frame.form_action or self.frame.url,
            "docUrl": self.frame.url,
        }


class _NavFrame:
    """A frame whose URL changes when its Continue button is clicked."""
    def __init__(self, url, nav_to=None, has_password_before_click=False,
                 form_action=None, formaction_attr=None):
        self.url, self._nav_to = url, nav_to
        self.has_password = has_password_before_click
        self.form_action = form_action        # <form action=...>, if not its own URL
        self.formaction_attr = formaction_attr  # a button's explicit formaction attr
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


# --- form action, not just frame URL ---------------------------------------
# Frame trust judges where a form *lives*; a trusted, never-navigating frame can
# still host <form action="https://attacker/"> and post the credentials there on
# submit — invisible to a frame-URL check. These pin the destination check.

def test_username_not_submitted_when_continue_posts_cross_origin(monkeypatch):
    """Two-step: the frame never navigates, but its Continue button submits the
    email form to an attacker origin. Must abort before the click."""
    main = _NavFrame("https://example.com/login",
                     form_action="https://attacker.example.net/harvest")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is False
    assert ("clicked", "submit") not in main.typed  # the continue click never happened


def test_credentials_not_submitted_when_one_step_form_posts_cross_origin(monkeypatch):
    """One-step: both fields present in a trusted frame, but the form posts to an
    attacker. The password re-check passes (frame is on example.com) — the
    destination check is what must stop it."""
    main = _NavFrame("https://example.com/login", has_password_before_click=True,
                     form_action="https://attacker.example.net/harvest")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is False
    assert ("submitted", None) not in main.typed


def test_same_site_form_action_still_submits(monkeypatch):
    """A form that posts back to its own site must still work."""
    main = _NavFrame("https://example.com/login", has_password_before_click=True,
                     form_action="https://example.com/auth")
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is True
    assert ("password", "SECRET") in main.typed


def test_submit_dest_unreadable_fails_closed(monkeypatch):
    """If the destination can't be read, don't submit credentials."""
    class _BrokenLoc(_Loc):
        async def evaluate(self, _js):
            raise RuntimeError("cannot evaluate")

    main = _NavFrame("https://example.com/login", has_password_before_click=True)
    page = _NavPage(main)

    async def first_visible(scope, selectors):
        frame = scope.main_frame if isinstance(scope, _NavPage) else scope
        sel = selectors[0]
        if "pass" in sel:
            return _BrokenLoc(frame, "password") if frame.has_password else None
        if "submit" in sel or "button" in sel:
            return _BrokenLoc(frame, "submit")
        return _BrokenLoc(frame, "username")
    monkeypatch.setattr(browser_login, "_first_visible", first_visible)

    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is False


# --- the DOM trap: formAction is never empty for a plain button -------------
# The first version of the destination check read `el.formAction || el.form.action`.
# That's dead code: a button with no `formaction` attribute still reports
# `el.formAction` == the document's own URL (its missing-value default), so the ||
# short-circuits on a trusted-looking URL and never reads the form's real action.
# Confirmed by review with a real cross-origin POST leaving the browser. These pin
# the corrected resolution — attribute presence, not the reflected property.

def test_resolve_submit_dest_reads_form_action_for_a_plain_button():
    """rev18's exact observed shape: no formaction attribute, formAction defaults to
    the page URL, the REAL destination sits in the form's action."""
    info = {
        "formactionAttr": None,
        "formAction": "http://127.0.0.1:8791/onestep_login.html",   # the trap: own URL
        "formAction_of_form": "http://localhost:8791/attacker.html",  # the real target
        "docUrl": "http://127.0.0.1:8791/onestep_login.html",
    }
    assert browser_login._resolve_submit_dest(info) == "http://localhost:8791/attacker.html"


def test_resolve_submit_dest_honours_explicit_formaction():
    info = {
        "formactionAttr": "https://attacker.example.net/x",
        "formAction": "https://attacker.example.net/x",
        "formAction_of_form": "https://example.com/auth",
        "docUrl": "https://example.com/login",
    }
    assert browser_login._resolve_submit_dest(info) == "https://attacker.example.net/x"


def test_resolve_submit_dest_no_form_falls_back_to_doc():
    info = {"formactionAttr": None, "formAction": "https://example.com/login",
            "formAction_of_form": None, "docUrl": "https://example.com/login"}
    assert browser_login._resolve_submit_dest(info) == "https://example.com/login"


def test_one_step_plain_button_cross_origin_form_is_rejected(monkeypatch):
    """End-to-end version of rev18's live exploit: a trusted, never-navigating frame
    with a one-step form whose action posts to an attacker, and a PLAIN submit button
    (no formaction attribute). The frame-URL check passes; only reading the form
    action stops it. This is the case the broken version shipped through."""
    main = _NavFrame("https://example.com/login", has_password_before_click=True,
                     form_action="https://attacker.example.net/harvest",
                     formaction_attr=None)  # <-- plain button, the realistic shape
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is False
    assert ("clicked", "submit") not in main.typed
    assert ("submitted", None) not in main.typed
    assert ("password", "SECRET") not in main.typed


def test_two_step_plain_continue_cross_origin_form_is_rejected(monkeypatch):
    """Same, for the two-step Continue button (the username-leak path)."""
    main = _NavFrame("https://example.com/login",
                     form_action="https://attacker.example.net/harvest",
                     formaction_attr=None)
    page = _NavPage(main)
    _install(monkeypatch, page)
    ok = run(browser_login._fill_and_submit(page, _recipe(), "victim@example.com", "SECRET"))
    assert ok is False
    assert ("clicked", "submit") not in main.typed


# --- the render tier is subject to the egress guard --------------------------

class _FakeRoute:
    """Records what the navigation guard decided about one request."""

    def __init__(self, url, navigation=True):
        self.request = self
        self.url = url
        self._navigation = navigation
        self.verdict = None

    def is_navigation_request(self):
        return self._navigation

    async def abort(self):
        self.verdict = "abort"

    async def continue_(self):
        self.verdict = "continue"


class _FakePage:
    async def route(self, _pattern, handler):
        self.handler = handler

    async def send(self, route):
        await self.handler(route, route.request)
        return route.verdict


def test_render_refuses_a_non_public_target(monkeypatch):
    """The browser tier has to honour the same egress policy as the HTTP tiers.

    Those go through ``egress.guarded_get``, which walks redirects itself so a
    public URL cannot bounce into a private range. This tier used to be reachable
    only for operator-curated paywall domains; an anti-bot wall now escalates
    arbitrary feed entry URLs into it, so the guard has to apply here too — and
    the target should be rejected before paying for a browser launch.
    """
    monkeypatch.setattr(browser_login, "_CHROMIUM_AVAILABLE", True)

    async def _never(*a, **kw):
        raise AssertionError("must not launch a browser for a blocked target")

    monkeypatch.setattr(browser_login, "_guard_navigations", _never)
    assert run(browser_login.render_page_html("http://127.0.0.1:9/x")) is None


def test_navigation_guard_aborts_a_redirect_into_a_private_range():
    """Chromium follows redirects internally, so the check runs per navigation."""
    page = _FakePage()
    run(browser_login._guard_navigations(page))
    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/x"):
        assert run(page.send(_FakeRoute(url))) == "abort", url


def test_navigation_guard_allows_a_public_navigation(monkeypatch):
    async def _resolves_public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress, "_resolve", _resolves_public)   # keep the test hermetic
    page = _FakePage()
    run(browser_login._guard_navigations(page))
    assert run(page.send(_FakeRoute("https://example.com/article"))) == "continue"


def test_navigation_guard_does_not_re_check_subresources():
    """Only a navigation can carry the page somewhere else; images cannot."""
    page = _FakePage()
    run(browser_login._guard_navigations(page))
    route = _FakeRoute("http://127.0.0.1/style.css", navigation=False)
    assert run(page.send(route)) == "continue"
