"""Server-side paywall login via a self-hosted headless Chromium.

Some paywalls (Piano/tinypass and similar) authenticate with JavaScript, so a
plain HTTP POST can't mint a session. We drive a real browser, perform the login,
and hand the resulting cookies back to the existing ``site_cookies`` plumbing.

We run Chromium *locally* (via ``playwright install chromium``) rather than on
BrightData's Scraping Browser: BrightData deliberately blocks typing into
password fields ("Forbidden action: password typing is not allowed"), which
makes credential login impossible there. A self-hosted browser has no such
restriction. If a paywall ever blocks this host's IP, route the browser through
``LOGIN_BROWSER_PROXY`` (a plain proxy — proxies don't block password entry).

Per-site quirks are *configuration*, loaded from ``LOGIN_RECIPES_FILE`` outside
the repo (the set of sites you can log into is the set of subscriptions you hold).
Everything else falls back to heuristic field detection across the page and its
*trusted* frames — the main frame, same-site frames, and the ``_AUTH_FRAME_HOSTS``
allowlist. Credentials are never typed into an arbitrary third-party iframe. On
failure we save a screenshot to ``/tmp/rssfeed-login-debug`` so a recipe can be
tuned live.
"""
import glob
import json
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app import egress
from app.config import LOGIN_BROWSER_PROXY, LOGIN_RECIPES_FILE

logger = logging.getLogger(__name__)

_DEBUG_DIR = "/tmp/rssfeed-login-debug"

# Cross-origin iframes we'll type credentials into. These paywall auth providers
# host the real login form themselves, so refusing all cross-origin frames would
# break login on the very sites this feature exists for. Anything not listed here
# (and not same-site) is never offered the user's credentials.
_AUTH_FRAME_HOSTS = (
    "tinypass.com",
    "piano.io",
)

# Realistic context so the headless browser isn't trivially fingerprinted.
_UA_TEMPLATE = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _ua_for(browser) -> str:
    """Claim the Chrome version we are actually running.

    A hardcoded version drifts from the binary on every playwright upgrade, and
    an anti-bot check that fingerprints the JS engine against the claimed UA
    reads that gap as a lie: this said Chrome 126 while the pinned build was 148.
    """
    major = (getattr(browser, "version", "") or "").split(".")[0]
    return _UA_TEMPLATE.format(major=major if major.isdigit() else "126")


def _playwright_package_root() -> str | None:
    """The driver ``package`` directory inside the installed playwright wheel.

    Imported defensively: this runs at module import time via
    ``_CHROMIUM_AVAILABLE``, and the browser tier is optional. Every other
    playwright import in this module is guarded for the same reason — a missing
    or broken wheel must degrade the tier, not raise through ``app.extractor``
    and take the whole reader down on import.
    """
    try:
        import playwright
    except Exception:
        return None
    return os.path.join(os.path.dirname(playwright.__file__), "driver", "package")


def _playwright_registry() -> list[dict] | None:
    """Playwright's own ``browsers.json`` — the registry it launches from."""
    root = _playwright_package_root()
    if root is None:
        return None
    try:
        with open(os.path.join(root, "browsers.json")) as fh:
            return json.load(fh)["browsers"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _browsers_base() -> str | None:
    """Where playwright keeps provisioned browsers, mirroring its own resolution.

    ``PLAYWRIGHT_BROWSERS_PATH=0`` is a documented *mode* — "install beside the
    package" — not a path. Taking it as one points the check at a relative
    directory named ``0``, matches nothing, and silently disables the browser
    tier on a machine where the browsers are installed correctly.
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        root = _playwright_package_root()
        return os.path.join(root, ".local-browsers") if root else None
    if env:
        return os.path.abspath(env)
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "ms-playwright")


# Both ``launch()`` calls in this module pass ``headless=True``, and playwright
# >=1.49 runs a *separate* ``chrome-headless-shell`` binary for that — the full
# chromium build is what a headed launch uses, which we never do. Ordered by what
# we would actually launch: the first entry the *registry* pins is the one that
# has to be on disk, so the full build serves only pre-1.49 playwrights that ship
# no shell at all. It must not stand in for a shell that is pinned but missing —
# that is precisely the cache that passes the check and then dies in ``launch()``.
# Inner globs cover the current ``chrome-headless-shell-linux64`` layout and the
# older ``chrome-linux/headless_shell`` one.
_LAUNCH_BINARIES = (
    ("chromium-headless-shell", "chromium_headless_shell-{rev}",
     ("chrome-headless-shell-*linux*/chrome-headless-shell", "chrome-*linux*/headless_shell")),
    ("chromium", "chromium-{rev}", ("chrome-*linux*/chrome",)),
)


def _pinned_revisions() -> dict[str, str]:
    """``{registry name: revision}`` for the browser builds this playwright pins."""
    return {
        b["name"]: str(b["revision"])
        for b in _playwright_registry() or []
        if b.get("name") and b.get("revision") is not None
    }


def _local_chromium_present() -> bool:
    """True if the exact browser build this playwright launches is provisioned.

    Two ways to get this wrong, and the previous versions managed one each.
    Globbing any ``chromium-*`` directory accepts a cache holding some *other*
    revision, which then dies inside ``launch()`` with "Executable doesn't
    exist". Pinning the revision but globbing ``chromium-<rev>/.../chrome``
    checks a binary we never run, so a headless-shell-only cache — a documented
    and much smaller install — reports the tier as missing when it works fine.
    """
    base = _browsers_base()
    if base is None:
        return False
    revisions = _pinned_revisions()
    launched = next(
        ((directory.format(rev=revisions[name]), inners)
         for name, directory, inners in _LAUNCH_BINARIES if name in revisions),
        None,
    )
    if launched is None:
        return False
    directory, inners = launched
    return any(glob.glob(os.path.join(base, directory, inner)) for inner in inners)


_CHROMIUM_AVAILABLE = _local_chromium_present()

# Heuristic selector candidates, tried in order, used when a recipe omits them.
_DEFAULT_USER_SEL = [
    "input[type=email]",
    "input[autocomplete=username]",
    "input[name*=email i]",
    "input[name*=user i]",
    "input[id*=email i]",
    "input[type=text][name*=login i]",
]
_DEFAULT_PASS_SEL = [
    "input[type=password]",
    "input[autocomplete=current-password]",
    "input[name*=pass i]",
]
_DEFAULT_SUBMIT_SEL = [
    "button[type=submit]",
    "input[type=submit]",
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
    "button:has-text('Log in')",
    "button:has-text('Log In')",
    "button:has-text('Continue')",
]


@dataclass
class LoginRecipe:
    login_url: str
    username_selectors: list[str] = field(default_factory=list)
    password_selectors: list[str] = field(default_factory=list)
    submit_selectors: list[str] = field(default_factory=list)


def _selector_list(spec: dict, key: str) -> list[str]:
    """A recipe's selector list, validated.

    Rejects a bare string explicitly: `"username_selectors": "#login-email"` is the
    natural thing to write instead of a list, and `list()` would silently splay it
    into ['#','l','o','g',...]. That "works" — no error, no warning — and then the
    login just mysteriously never finds the field. Better to skip the recipe loudly.
    """
    v = spec.get(key) or []
    if not isinstance(v, list):
        raise TypeError(f"{key} must be a list of CSS selectors")
    if not all(isinstance(s, str) for s in v):
        raise TypeError(f"{key} must contain only strings")
    return v


def load_recipes(path: str) -> dict[str, LoginRecipe]:
    """Load per-site login overrides from a JSON file.

    Recipes are *configuration*, not code: the set of sites you can log into is
    the set of subscriptions you pay for — data about the operator, not program
    logic. So they live outside the repo (see ``LOGIN_RECIPES_FILE`` and
    ``config/login-recipes.example.json``) and none ship here. With no file every
    site uses the generic heuristic path and the app works unchanged.

    A recipe is only needed where the heuristics misfire — an ad-heavy login page
    can carry several `button[type=submit]`, and the generic selector will happily
    click the wrong one; pinning explicit selectors fixes that.

    A malformed entry is skipped on its own rather than failing the whole load, so
    one bad recipe can't disable the others. Warnings name the domain only, never
    the recipe body.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise TypeError("top level must be an object of domain -> recipe")
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("login recipes: ignoring %s (%s)", path, exc)
        return {}

    out: dict[str, LoginRecipe] = {}
    for domain, spec in raw.items():
        if str(domain).startswith("__"):
            continue  # JSON has no comments; `__`-prefixed keys are documentation
        try:
            url = spec["login_url"]
            if not isinstance(url, str) or not url.strip():
                raise TypeError("login_url must be a non-empty string")
            out[str(domain).lower()] = LoginRecipe(
                login_url=url,
                username_selectors=_selector_list(spec, "username_selectors"),
                password_selectors=_selector_list(spec, "password_selectors"),
                submit_selectors=_selector_list(spec, "submit_selectors"),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            logger.warning("login recipes: skipping %s (%s)", domain, exc)
    if out:
        logger.info("login recipes: loaded %d", len(out))
    return out


LOGIN_RECIPES: dict[str, LoginRecipe] = load_recipes(LOGIN_RECIPES_FILE)


def recipe_for(domain: str) -> LoginRecipe:
    bare = domain.removeprefix("www.")
    return (
        LOGIN_RECIPES.get(domain)
        or LOGIN_RECIPES.get(bare)
        or LoginRecipe(login_url=f"https://{domain}/login")
    )


def has_login_recipe(domain: str | None) -> bool:
    """True only for domains with a site-specific recipe (a known paywall site).

    Unlike ``recipe_for`` (which always returns a heuristic fallback), this is the
    signal for "is subscription login meaningful here?" — used to decide whether to
    surface a login affordance in the reader.
    """
    if not domain:
        return False
    return domain in LOGIN_RECIPES or domain.removeprefix("www.") in LOGIN_RECIPES


def login_available() -> bool:
    """True if the self-hosted login browser is provisioned (chromium installed)."""
    return _CHROMIUM_AVAILABLE


def _cookies_for_domain(cookies: list[dict], domain: str) -> dict[str, str]:
    bare = domain.removeprefix("www.")
    jar: dict[str, str] = {}
    for c in cookies:
        cd = (c.get("domain") or "").lstrip(".")
        if cd == domain or cd == bare or cd.endswith("." + bare):
            jar[c["name"]] = c["value"]
    return jar


async def _first_visible(scope, selectors: list[str]):
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _frame_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _same_site(host: str, target: str) -> bool:
    """Same registrable-ish site: exact host, or a subdomain of the target.

    `www.` is stripped from the target so a recipe pointing at
    ``https://www.example.com/login`` still trusts a form frame on the bare
    ``example.com`` — the same site by any sane reading; refusing it would break
    real logins and buy no safety.
    """
    if not host or not target:
        return False
    target = target.removeprefix("www.")
    return host == target or host.endswith("." + target)


def _host_trusted(host: str, target: str) -> bool:
    """May this host be shown the user's credentials?"""
    return _same_site(host, target) or any(_same_site(host, p) for p in _AUTH_FRAME_HOSTS)


def frame_trusted_now(frame, target: str) -> bool:
    """Is this frame trusted *at this instant*, judged by its current URL?

    Deliberately re-read rather than remembered. A Frame is a live handle:
    navigating it swaps the document but keeps the object valid, so a trust
    decision made when the frame was listed says nothing about where it is by the
    time we type into it.
    """
    return _host_trusted(_frame_host(frame.url), target)


# The JS gathers raw facts only; the trust DECISION lives in Python (below), where
# it can be tested against realistic DOM-shaped data. That split is deliberate: the
# first version of this check baked the decision into the JS as
# `el.formAction || el.form.action`, which is a dead no-op — `el.formAction`
# reflects the document's own URL as its missing-value default when the button has
# no `formaction` attribute (HTML spec), so the `||` short-circuits on a
# trusted-looking URL and never reads the form's real action. A Python mock of
# `evaluate` couldn't catch it because the mock encoded the same wrong assumption;
# only a real browser did. So: read the ATTRIBUTE presence, not the reflected
# property, and decide in code we can actually test.
_SUBMIT_DEST_JS = """el => ({
    formactionAttr: el.getAttribute('formaction'),
    formAction: el.formAction || null,
    formAction_of_form: (el.form && el.form.action) || null,
    docUrl: location.href,
})"""


def _resolve_submit_dest(info: dict) -> str:
    """The effective form-submission destination, from raw DOM facts.

    A submit button's own ``formaction`` overrides the form — but only when the
    *attribute* is actually set; the reflected ``formAction`` property is never
    empty (it defaults to the page URL), so attribute presence is what decides.
    Otherwise the enclosing form's ``action`` is the destination (itself defaulting
    to the document URL when unset, which is correct — a form with no action posts
    to where it already is). No form at all ⇒ a JS-only button ⇒ current document.
    """
    if info.get("formactionAttr"):
        return info.get("formAction") or ""
    return info.get("formAction_of_form") or info.get("docUrl") or ""


async def _submit_dest_trusted(handle, target: str) -> bool:
    """Would activating this element submit a form to a trusted origin?

    Frame trust judges where a form *lives*; this judges where it *posts*. They
    differ: a trusted, never-navigating frame can host ``<form action="https://
    attacker/">``, and submitting it sends the credentials there while the frame's
    URL never changes — invisible to frame_trusted_now. So before any click that can
    submit, resolve the effective destination and check its host.

    Fails CLOSED: if the destination can't be read, we don't submit credentials.

    Residual: a JS onsubmit/click handler that fetch()es the fields to an arbitrary
    endpoint exposes no inspectable destination and can't be caught here.
    """
    try:
        info = await handle.evaluate(_SUBMIT_DEST_JS)
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    return _host_trusted(_frame_host(_resolve_submit_dest(info)), target)


def trusted_login_frames(page, login_url: str) -> list:
    """The frames we're willing to type credentials into, judged right now.

    The login form legitimately lives in a cross-origin iframe on some paywalls
    (Piano/tinypass hosts the form itself), so we can't simply refuse all of them.
    But scanning *every* frame means a third-party iframe that merely happens to
    contain matching inputs — a newsletter widget's `input[type=email]`, say —
    can win the heuristic race and receive the user's real subscription
    credentials, which are then submitted to that third party. The user just sees
    "login failed" and never learns where their details went.

    So: same-site frames plus an explicit allowlist of known auth providers.

    The main frame gets **no** special pass. It used to be trusted
    unconditionally, which meant that once a click navigated it off-site it stayed
    "trusted" and would still be handed the password. It is judged by its current
    URL like every other frame.
    """
    target = _frame_host(login_url)
    out = []
    for f in page.frames:  # page.frames[0] is the main frame
        if frame_trusted_now(f, target):
            out.append(f)
        elif _frame_host(f.url):
            logger.debug("skipping untrusted login frame: %s", _frame_host(f.url))
    return out


async def _fill_and_submit(page, recipe: LoginRecipe, username: str, password: str) -> bool:
    user_sel = recipe.username_selectors or _DEFAULT_USER_SEL
    pass_sel = recipe.password_selectors or _DEFAULT_PASS_SEL
    submit_sel = recipe.submit_selectors or _DEFAULT_SUBMIT_SEL

    target = _frame_host(recipe.login_url)

    # Only same-site frames and known auth providers — never an arbitrary
    # third-party iframe. See trusted_login_frames().
    for scope in trusted_login_frames(page, recipe.login_url):
        user = await _first_visible(scope, user_sel)
        if not user:
            continue
        if not frame_trusted_now(scope, target):
            continue  # drifted between listing and now
        # Never type a credential into a form that posts cross-origin — check the
        # destination *before* filling, not just before submitting. Filling alone
        # doesn't leak, but this keeps the username out of the DOM of a form bound
        # for an attacker, and it's the earliest point we can refuse.
        if not await _submit_dest_trusted(user, target):
            logger.warning("Aborting login for %s — the login form submits to an "
                           "untrusted origin", recipe.login_url)
            return False
        await user.fill(username)

        # Track which frame the password field came from — it isn't always `scope`
        # (the two-step path falls back to the main frame), and the trust re-check
        # has to apply to the frame we're actually about to type into.
        pw, pw_frame = await _first_visible(scope, pass_sel), scope

        # Two-step flows (email → Continue → password) reveal the password later.
        # This click can NAVIGATE — an SSO bounce, an open redirect, a hop through
        # an auth provider — so where we land is not where we vetted.
        if not pw:
            cont = await _first_visible(scope, submit_sel)
            if cont:
                # The click submits the current form, carrying the username we just
                # typed. If that form posts cross-origin the username leaks — and no
                # post-click check can help, because the click IS the navigation. So
                # vet the destination first and abort before clicking if untrusted.
                if not await _submit_dest_trusted(cont, target):
                    logger.warning(
                        "Aborting login for %s — the continue step submits to an "
                        "untrusted origin", recipe.login_url)
                    return False
                await cont.click()
                await page.wait_for_timeout(2500)
                if frame_trusted_now(scope, target):
                    pw, pw_frame = await _first_visible(scope, pass_sel), scope
                if not pw and frame_trusted_now(page.main_frame, target):
                    pw, pw_frame = await _first_visible(page, pass_sel), page.main_frame
        if not pw:
            continue

        # The password is about to be typed: re-verify the frame holding the field,
        # whatever happened above.
        if not frame_trusted_now(pw_frame, target):
            logger.warning(
                "Aborting login for %s — the form moved to an untrusted origin (%s)",
                recipe.login_url, _frame_host(pw_frame.url) or "unknown",
            )
            return False
        # Same guard as for the username: don't type the password into a form whose
        # action posts cross-origin, even though the frame it lives in is trusted.
        if not await _submit_dest_trusted(pw, target):
            logger.warning("Aborting login for %s — the login form submits to an "
                           "untrusted origin", recipe.login_url)
            return False
        await pw.fill(password)

        # Submit from the frame holding the password, not from `scope`. They're the
        # same in the ordinary case; where they differ (the two-step fallback found
        # the field on the main frame) the password's own form is the one we mean to
        # submit — looking in a `scope` that may have drifted would mean clicking a
        # button on someone else's page and then reporting success.
        submit = await _first_visible(pw_frame, submit_sel)
        if not frame_trusted_now(pw_frame, target):
            logger.warning("Aborting login for %s — origin changed before submit",
                           recipe.login_url)
            return False
        # Also vet where the form posts, not just where it lives: a trusted frame
        # can still carry a cross-origin form action that would send both fields to
        # an attacker. Check the submit button (or, for the Enter fallback, the
        # password field's own form).
        if not await _submit_dest_trusted(submit or pw, target):
            logger.warning("Aborting login for %s — the login form submits to an "
                           "untrusted origin", recipe.login_url)
            return False
        if submit:
            await submit.click()
        else:
            await pw.press("Enter")
        logger.info("Submitted login form for %s (scope=%s)", recipe.login_url,
                    _frame_host(pw_frame.url) or "page")
        return True
    return False


async def _dump_failure(page, domain: str, reason: str) -> None:
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        path = f"{_DEBUG_DIR}/{domain.replace('/', '_')}-{reason}.png"
        await page.screenshot(path=path, full_page=True)
        logger.warning("Login failed for %s (%s) — screenshot at %s", domain, reason, path)
    except Exception:
        logger.warning("Login failed for %s (%s); screenshot capture also failed", domain, reason)


async def login_and_get_cookies(domain: str, username: str, password: str) -> dict[str, str] | None:
    """Drive a browser login for ``domain`` and return its cookies, or None on failure."""
    if not _CHROMIUM_AVAILABLE:
        logger.warning("Local Chromium not installed — run `playwright install chromium`")
        return None

    recipe = recipe_for(domain)
    proxy = {"server": LOGIN_BROWSER_PROXY} if LOGIN_BROWSER_PROXY else None
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=_ua_for(browser), viewport={"width": 1280, "height": 900}, proxy=proxy,
                )
                page = await context.new_page()
                await page.goto(recipe.login_url, wait_until="domcontentloaded", timeout=90_000)

                try:
                    if not await _fill_and_submit(page, recipe, username, password):
                        await _dump_failure(page, domain, "fields-not-found")
                        return None
                except Exception:
                    # A fill/click that raises (e.g. a selector resolved but the action
                    # failed) must still leave a screenshot — that's where tuning starts.
                    logger.exception("Login interaction failed for %s", domain)
                    await _dump_failure(page, domain, "interaction-error")
                    return None

                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass

                # A still-visible password field means we're back on the login form —
                # i.e. the credentials were rejected. Without this, the anonymous
                # cookies a paywall sets anyway would masquerade as a real session.
                pass_sel = recipe.password_selectors or _DEFAULT_PASS_SEL
                if await _first_visible(page, pass_sel) is not None:
                    await _dump_failure(page, domain, "login-rejected")
                    logger.warning("Login for %s appears rejected (still on login form)", domain)
                    return None

                jar = _cookies_for_domain(await context.cookies(), domain)
                if not jar:
                    await _dump_failure(page, domain, "no-cookies")
                    return None

                logger.info("Browser login for %s captured %d cookies", domain, len(jar))
                return jar
            finally:
                await browser.close()
    except Exception:
        logger.exception("Browser login crashed for %s", domain)
        return None


# Article containers that signal the SPA has hydrated real prose (not just chrome).
_RENDER_READY_JS = (
    "() => document.querySelectorAll("
    "'article p, #page p, main p, .article__content p, .article-content p'"
    ").length >= 2"
)


# Fingerprints of an *unsolved* anti-bot interstitial. Returning one of these as
# article HTML is worse than returning nothing: "Just a quick check..." then gets
# extracted, stored as the article body and shown in the reader as the news.
#
# Deliberately narrow. ``cdn-cgi/challenge-platform`` looks like an obvious third
# marker and is not one: Cloudflare's JavaScript-detections beacon is served from
# that path into ordinary 200s on any bot-management zone, and is still on the
# page you land on *after* solving a challenge. Matching it would discard
# successful renders on exactly the sites this tier exists for.
_INTERSTITIAL_MARKERS = (
    "_cf_chl_opt",
    'id="challenge-form"',
)


def is_interstitial(html: str) -> bool:
    """True if ``html`` is an anti-bot challenge page rather than content.

    Public because the HTTP tiers need it too: Wayback happily returns 200 with a
    *snapshot of the challenge page*, which would otherwise be stored as the
    article.
    """
    return any(m in html for m in _INTERSTITIAL_MARKERS)


async def _guard_navigations(page) -> None:
    """Refuse to let the browser follow a redirect into a private address.

    Every HTTP tier goes through ``egress.guarded_get``, which walks redirects
    itself with ``follow_redirects=False`` precisely so a public URL cannot bounce
    the fetch into a private range via a 30x ``Location``. Chromium follows
    redirects internally, so that check has to be re-applied here — and it matters
    more now than it did: this tier used to be reachable only for operator-curated
    paywall domains, and an anti-bot wall now escalates arbitrary feed entry URLs
    into it. Without this, a hostile feed entry pointing at a host that 403s and
    then redirects to ``169.254.169.254`` would be rendered and stored as article
    text.
    """
    async def _route(route, request):
        if request.is_navigation_request():
            try:
                await egress.check_public(request.url)
            except egress.EgressBlockedError as exc:
                logger.warning("Refusing browser navigation to %s: %s", request.url, exc)
                await route.abort()
                return
        await route.continue_()

    await page.route("**/*", _route)


async def render_page_html(url: str, cookies: dict[str, str] | None = None, *,
                           settle_ms: int = 4000, timeout: int = 60_000,
                           prose_budget_s: float = 20.0) -> str | None:
    """Render a JS/SPA page in a real browser (with session cookies) and return its
    HTML. On a paywalled SPA the article only exists after JS runs, so plain httpx
    returns an empty shell — this is the fetch tier that works.
    """
    if not _CHROMIUM_AVAILABLE:
        return None
    from urllib.parse import urlparse

    from playwright.async_api import async_playwright

    # Reject an unroutable target before paying for a browser launch; redirects
    # away from a public URL are caught in-flight by _guard_navigations.
    try:
        await egress.check_public(url)
    except egress.EgressBlockedError as exc:
        logger.warning("Refusing to render %s: %s", url, exc)
        return None

    proxy = {"server": LOGIN_BROWSER_PROXY} if LOGIN_BROWSER_PROXY else None
    bare = (urlparse(url).hostname or "").removeprefix("www.")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=_ua_for(browser), viewport={"width": 1280, "height": 1600}, proxy=proxy,
                )
                if cookies and bare:
                    await context.add_cookies([
                        {"name": k, "value": v, "domain": "." + bare, "path": "/"}
                        for k, v in cookies.items()
                    ])
                page = await context.new_page()
                await _guard_navigations(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                # Wait for article prose to hydrate; fall back to a fixed settle.
                # One wait is enough even when an anti-bot interstitial solves
                # itself and navigates: wait_for_function re-evaluates against
                # the new document (measured — it resolves ~1s after a
                # cross-document navigation), so there is nothing to re-arm.
                try:
                    await page.wait_for_function(_RENDER_READY_JS,
                                                 timeout=prose_budget_s * 1000)
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                html = await page.content()
                if is_interstitial(html):
                    logger.warning(
                        "Browser render for %s is still an anti-bot interstitial "
                        "after %.0fs — discarding rather than storing it as article text",
                        url, prose_budget_s)
                    return None
                return html
            finally:
                await browser.close()
    except Exception:
        logger.exception("Browser render failed for %s", url)
        return None
