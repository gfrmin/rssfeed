"""Server-side paywall login via a self-hosted headless Chromium.

Some paywalls (e.g. National Review's Piano/tinypass flow) authenticate with
JavaScript, so a plain HTTP POST can't mint a session. We drive a real browser,
perform the login, and hand the resulting cookies back to the existing
``site_cookies`` plumbing.

We run Chromium *locally* (via ``playwright install chromium``) rather than on
BrightData's Scraping Browser: BrightData deliberately blocks typing into
password fields ("Forbidden action: password typing is not allowed"), which
makes credential login impossible there. A self-hosted browser has no such
restriction. If a paywall ever blocks steel's IP, route the browser through
``LOGIN_BROWSER_PROXY`` (a plain proxy — proxies don't block password entry).

Per-site quirks live in ``LOGIN_RECIPES``; everything else falls back to
heuristic field detection across the page and its iframes. On failure we save a
screenshot to ``/tmp/rssfeed-login-debug`` so the recipe can be tuned live.
"""
import glob
import logging
import os
from dataclasses import dataclass, field

from app.config import LOGIN_BROWSER_PROXY

logger = logging.getLogger(__name__)

_DEBUG_DIR = "/tmp/rssfeed-login-debug"

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


# Per-site overrides. NR first; the heuristic path covers the common case.
# NR's /login/ is a plain server-rendered form, but the page is ad-heavy, so the
# generic `button[type=submit]` heuristic latched onto the wrong button — hence the
# explicit selectors below (verified against the live form).
LOGIN_RECIPES: dict[str, LoginRecipe] = {
    "nationalreview.com": LoginRecipe(
        login_url="https://www.nationalreview.com/login/",
        username_selectors=["#login-email"],
        password_selectors=["#login-password"],
        submit_selectors=["button.login__button--login", "button[type=submit].login__button"],
    ),
}


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


async def _fill_and_submit(page, recipe: LoginRecipe, username: str, password: str) -> bool:
    user_sel = recipe.username_selectors or _DEFAULT_USER_SEL
    pass_sel = recipe.password_selectors or _DEFAULT_PASS_SEL
    submit_sel = recipe.submit_selectors or _DEFAULT_SUBMIT_SEL

    # The login form may live in the page or any (Piano) iframe.
    for scope in [page, *page.frames]:
        user = await _first_visible(scope, user_sel)
        if not user:
            continue
        await user.fill(username)
        pw = await _first_visible(scope, pass_sel)
        # Two-step flows (email → Continue → password) reveal the password later.
        if not pw:
            cont = await _first_visible(scope, submit_sel)
            if cont:
                await cont.click()
                await page.wait_for_timeout(2500)
                pw = await _first_visible(scope, pass_sel) or await _first_visible(page, pass_sel)
        if not pw:
            continue
        await pw.fill(password)
        submit = await _first_visible(scope, submit_sel)
        if submit:
            await submit.click()
        else:
            await pw.press("Enter")
        logger.info("Submitted login form for %s (scope=%s)", recipe.login_url,
                    "page" if scope is page else "frame")
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
    HTML. For paywalled SPAs like National Review, the article only exists after JS
    runs, so plain httpx returns an empty shell — this is the fetch tier that works.
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
