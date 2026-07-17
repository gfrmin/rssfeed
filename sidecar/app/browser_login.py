"""Server-side paywall login via a self-hosted headless Chromium.

Some paywalls (Piano/tinypass and similar) authenticate with JavaScript, so a
plain HTTP POST can't mint a session. We drive a real browser, perform the login,
and hand the resulting cookies back to the existing ``site_cookies`` plumbing.

We run Chromium *locally* (via ``playwright install chromium``) rather than on
BrightData's Scraping Browser: BrightData deliberately blocks typing into
password fields ("Forbidden action: password typing is not allowed"), which
makes credential login impossible there. A self-hosted browser has no such
restriction. If a paywall ever blocks steel's IP, route the browser through
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
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _local_chromium_present() -> bool:
    """True if `playwright install chromium` has provisioned a browser binary."""
    # chrome-linux on official builds, chrome-linux64 on the OS-fallback build.
    base = os.path.expanduser("~/.cache/ms-playwright")
    return bool(
        glob.glob(os.path.join(base, "chromium-*/chrome-linux*/chrome"))
        or glob.glob(os.path.join(base, "chromium_headless_shell-*/chrome-linux*/headless_shell"))
    )


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
                    user_agent=_UA, viewport={"width": 1280, "height": 900}, proxy=proxy,
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


async def render_page_html(url: str, cookies: dict[str, str] | None = None, *,
                           settle_ms: int = 4000, timeout: int = 60_000) -> str | None:
    """Render a JS/SPA page in a real browser (with session cookies) and return its
    HTML. On a paywalled SPA the article only exists after JS runs, so plain httpx
    returns an empty shell — this is the fetch tier that works.
    """
    if not _CHROMIUM_AVAILABLE:
        return None
    from urllib.parse import urlparse

    from playwright.async_api import async_playwright

    proxy = {"server": LOGIN_BROWSER_PROXY} if LOGIN_BROWSER_PROXY else None
    bare = (urlparse(url).hostname or "").removeprefix("www.")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=_UA, viewport={"width": 1280, "height": 1600}, proxy=proxy,
                )
                if cookies and bare:
                    await context.add_cookies([
                        {"name": k, "value": v, "domain": "." + bare, "path": "/"}
                        for k, v in cookies.items()
                    ])
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                # Wait for article prose to hydrate; fall back to a fixed settle.
                try:
                    await page.wait_for_function(_RENDER_READY_JS, timeout=20_000)
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                return await page.content()
            finally:
                await browser.close()
    except Exception:
        logger.exception("Browser render failed for %s", url)
        return None
