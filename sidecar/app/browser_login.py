"""Server-side paywall login via the BrightData Scraping Browser.

Some paywalls (e.g. National Review's Piano/tinypass flow) authenticate with
JavaScript and render as a SPA, so a plain HTTP POST can't mint a session. We
drive a real remote browser over CDP, perform the login, and hand the resulting
cookies back to the existing ``site_cookies`` plumbing.

Per-site quirks live in ``LOGIN_RECIPES``; everything else falls back to
heuristic field detection across the page and its iframes (Piano renders its
form in an iframe). On failure we save a screenshot to ``/tmp/rssfeed-login-debug``
so the recipe can be tuned against the live flow.
"""
import logging
import os
from dataclasses import dataclass, field

from app.config import BRIGHTDATA_BROWSER_WSS

logger = logging.getLogger(__name__)

_DEBUG_DIR = "/tmp/rssfeed-login-debug"

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
LOGIN_RECIPES: dict[str, LoginRecipe] = {
    "nationalreview.com": LoginRecipe(
        login_url="https://www.nationalreview.com/login/",
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
    """True if browser login is configured (the Scraping Browser endpoint is set)."""
    return bool(BRIGHTDATA_BROWSER_WSS)


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
    if not BRIGHTDATA_BROWSER_WSS:
        logger.warning("BRIGHTDATA_BROWSER_WSS unset — browser login unavailable")
        return None

    recipe = recipe_for(domain)
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(BRIGHTDATA_BROWSER_WSS, timeout=60_000)
            try:
                page = await browser.new_page()
                await page.goto(recipe.login_url, wait_until="domcontentloaded", timeout=90_000)

                if not await _fill_and_submit(page, recipe, username, password):
                    await _dump_failure(page, domain, "fields-not-found")
                    return None

                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass

                jar = _cookies_for_domain(await page.context.cookies(), domain)
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
