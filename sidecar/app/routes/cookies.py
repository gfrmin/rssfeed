import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------- Firefox cookie reader ----------

_FIREFOX_DIR = Path.home() / ".mozilla" / "firefox"


def _find_firefox_cookies_db() -> Path | None:
    """Find the default Firefox profile's cookies.sqlite."""
    if not _FIREFOX_DIR.is_dir():
        return None
    # Prefer profiles containing 'default-release', fall back to any with cookies.sqlite
    candidates = sorted(
        (p for p in _FIREFOX_DIR.iterdir() if p.is_dir() and (p / "cookies.sqlite").exists()),
        key=lambda p: ("default-release" not in p.name, p.name),
    )
    return candidates[0] / "cookies.sqlite" if candidates else None


def read_firefox_cookies(domain: str) -> dict[str, str]:
    """Read cookies for a domain from Firefox's cookie store."""
    db_path = _find_firefox_cookies_db()
    if not db_path:
        return {}

    # Copy DB + WAL/SHM to temp dir to avoid locking issues with running Firefox
    with tempfile.TemporaryDirectory() as tmpdir:
        for suffix in ("", "-wal", "-shm"):
            src = db_path.parent / (db_path.name + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmpdir) / (db_path.name + suffix))

        tmp_db = Path(tmpdir) / db_path.name
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host IN (?, ?, ?) AND expiry > strftime('%s', 'now')",
                (domain, f".{domain}", f"www.{domain}"),
            )
            return {name: value for name, value in cur.fetchall()}
        finally:
            conn.close()


def _parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse a 'key=value; key2=value2' cookie string into a dict."""
    cookies = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


async def get_cookies_for_url(url: str) -> dict[str, str] | None:
    """Look up stored cookies matching the domain of a URL."""
    domain = urlparse(url).hostname or ""
    if not domain:
        return None
    async with get_conn() as conn:
        # Try exact match first, then bare domain (strip www.)
        cur = await conn.execute(
            "SELECT cookies FROM site_cookies WHERE domain = %s OR domain = %s LIMIT 1",
            (domain, domain.removeprefix("www.")),
        )
        row = await cur.fetchone()
        return row["cookies"] if row else None


@router.get("/cookies", response_class=HTMLResponse)
async def cookie_list(request: Request):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT domain, cookies, updated_at FROM site_cookies ORDER BY domain"
        )
        rows = await cur.fetchall()
    return templates.TemplateResponse(request, "cookies.html", {"entries": rows})


@router.post("/cookies", response_class=HTMLResponse)
async def save_cookies(domain: str = Form(...), cookies_raw: str = Form(...)):
    domain = domain.strip().lower()
    if not domain:
        return HTMLResponse('<span class="error">Domain is required</span>', status_code=400)
    cookies = _parse_cookie_string(cookies_raw)
    if not cookies:
        return HTMLResponse('<span class="error">No valid cookies found</span>', status_code=400)

    import psycopg.types.json
    async with get_conn() as conn:
        await conn.execute(
            """INSERT INTO site_cookies (domain, cookies)
               VALUES (%s, %s::jsonb)
               ON CONFLICT (domain) DO UPDATE SET cookies = %s::jsonb, updated_at = NOW()""",
            (domain, psycopg.types.json.Json(cookies), psycopg.types.json.Json(cookies)),
        )
        await conn.commit()
    return HTMLResponse(f'<span class="success">Saved {len(cookies)} cookies for {domain}</span>')


@router.post("/cookies/from-firefox", response_class=HTMLResponse)
async def import_from_firefox(domain: str = Form(...)):
    domain = domain.strip().lower()
    if not domain:
        return HTMLResponse('<span class="error">Domain is required</span>', status_code=400)
    cookies = read_firefox_cookies(domain)
    if not cookies:
        return HTMLResponse(f'<span class="error">No Firefox cookies found for {domain}</span>')

    import psycopg.types.json
    async with get_conn() as conn:
        await conn.execute(
            """INSERT INTO site_cookies (domain, cookies)
               VALUES (%s, %s::jsonb)
               ON CONFLICT (domain) DO UPDATE SET cookies = %s::jsonb, updated_at = NOW()""",
            (domain, psycopg.types.json.Json(cookies), psycopg.types.json.Json(cookies)),
        )
        await conn.commit()
    return HTMLResponse(
        f'<span class="success">Imported {len(cookies)} cookies for {domain} from Firefox</span>'
    )


@router.post("/cookies/{domain}/delete", response_class=HTMLResponse)
async def delete_cookies(domain: str):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM site_cookies WHERE domain = %s", (domain,))
        await conn.commit()
    return HTMLResponse(f'<span class="success">Deleted cookies for {domain}</span>')
