"""Which frames may receive the user's subscription credentials.

The heuristic login path matches generic selectors (`input[type=email]`,
`input[name*=user i]`) against frames. Scanning every frame means a third-party
iframe that merely happens to contain matching inputs — a newsletter widget, an
ad unit — can win that race and be handed real credentials, which the code then
submits to it. These tests pin down that only trusted frames are ever offered.
"""
from app import browser_login


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
