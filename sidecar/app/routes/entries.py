import asyncio
import difflib
import logging
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import miniflux_client
from app.db import get_conn
from app.extractor import fetch_and_extract
from app.routes.cookies import get_cookies_for_url
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_snapshot(conn, entry_id: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM article_snapshots WHERE entry_id = %s ORDER BY version DESC LIMIT 1",
        (entry_id,),
    )
    return await cur.fetchone()


async def _version_count(conn, entry_id: int) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM article_snapshots WHERE entry_id = %s",
        (entry_id,),
    )
    row = await cur.fetchone()
    return row["cnt"] if row else 0


async def _feed_priorities(conn) -> dict[int, int]:
    cur = await conn.execute("SELECT feed_id, priority FROM feed_config")
    return {row["feed_id"]: row["priority"] for row in await cur.fetchall()}


async def _entries_with_changes(conn) -> set[int]:
    """Get entry IDs that have more than one snapshot version."""
    cur = await conn.execute(
        "SELECT entry_id FROM article_snapshots GROUP BY entry_id HAVING COUNT(*) > 1"
    )
    return {row["entry_id"] for row in await cur.fetchall()}


def _time_filter_params(time_filter: str | None) -> dict[str, str]:
    """Convert a time filter name to after/before timestamps."""
    if not time_filter:
        return {}
    now = datetime.now(timezone.utc)
    if time_filter == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "24h":
        start = now - timedelta(hours=24)
    elif time_filter == "week":
        start = now - timedelta(days=7)
    else:
        return {}
    return {"after": str(int(start.timestamp()))}


# ============================================================
#  Three-pane shell: sidebar context + list enrichment
# ============================================================

TIERS = [(1, "Must read"), (2, "Normal"), (3, "Low priority")]

VIEW_TITLES = {
    "unread": "Unread", "all": "All articles", "read": "Read", "starred": "Starred",
    "changed": "Changed",
    "t:today": "Today", "t:24h": "Last 24 hours", "t:week": "This week",
}

_STALE_SECONDS = 24 * 3600


def _feed_health_state(feed: dict, now: datetime) -> str:
    """Collapse Miniflux feed state into a dot state: ok/stale/error/paused."""
    if feed.get("disabled"):
        return "paused"
    if feed.get("parsing_error_message") or (feed.get("parsing_error_count") or 0) >= 1:
        return "error"
    checked = feed.get("checked_at", "")
    if checked:
        try:
            dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            if (now - dt).total_seconds() > _STALE_SECONDS:
                return "stale"
        except Exception:
            pass
    return "ok"


async def _snapshot_versions(conn, entry_ids: list[int]) -> dict[int, dict]:
    """Map entry_id -> {version, count} for entries that have full-text snapshots."""
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT entry_id, MAX(version) AS v, COUNT(*) AS c "
        "FROM article_snapshots WHERE entry_id = ANY(%s) GROUP BY entry_id",
        (entry_ids,),
    )
    return {r["entry_id"]: {"version": r["v"], "count": r["c"]} for r in await cur.fetchall()}


def _audio_enclosure(entry: dict) -> dict | None:
    for enc in entry.get("enclosures") or []:
        if (enc.get("mime_type") or "").startswith("audio"):
            return enc
    return None


async def _enrich_entries(conn, entries: list[dict], now: datetime) -> None:
    """Attach _has_changes, _priority, _full, _audio to each entry in place."""
    entry_ids = [e["id"] for e in entries]
    changed_ids = await _entries_with_changes(conn)
    snaps = await _snapshot_versions(conn, entry_ids)
    priorities = await _feed_priorities(conn)
    for e in entries:
        e["_has_changes"] = e["id"] in changed_ids
        e["_priority"] = priorities.get(e.get("feed_id"), 2)
        e["_full"] = snaps.get(e["id"])
        e["_audio"] = _audio_enclosure(e)


def _active_view(*, feed_id, view, starred, changed, time_filter, show_all) -> str | None:
    if feed_id:
        return None
    if view in VIEW_TITLES:
        return view
    if starred:
        return "starred"
    if changed:
        return "changed"
    if time_filter in ("today", "24h", "week"):
        return "t:" + time_filter
    if show_all:
        return "all"
    return "unread"


_SIDEBAR_CACHE: dict[str, tuple[float, dict]] = {}
_SIDEBAR_CACHE_TTL = 10.0
_SIDEBAR_CACHE_LOCK = asyncio.Lock()
_SIDEBAR_KEY = "tier"


def _invalidate_sidebar_cache() -> None:
    """Drop cached sidebar data so the next navigation recomputes counts/feeds.
    Call after any action that changes unread/read/starred counts."""
    _SIDEBAR_CACHE.clear()


async def _sidebar_data() -> dict:
    """Cached view-independent sidebar data (TTL + double-checked lock). Collapses the
    Miniflux calls + DB queries to a warm-cache hit during rapid navigation."""
    now_m = time.monotonic()
    cached = _SIDEBAR_CACHE.get(_SIDEBAR_KEY)
    if cached and now_m - cached[0] < _SIDEBAR_CACHE_TTL:
        return cached[1]
    async with _SIDEBAR_CACHE_LOCK:
        cached = _SIDEBAR_CACHE.get(_SIDEBAR_KEY)
        if cached and time.monotonic() - cached[0] < _SIDEBAR_CACHE_TTL:
            return cached[1]
        data = await _build_sidebar_data()
        _SIDEBAR_CACHE[_SIDEBAR_KEY] = (time.monotonic(), data)
        return data


async def build_sidebar(*, active_view: str | None, active_feed_id: int | None) -> dict:
    """Assemble the persistent left-rail context. The expensive, view-independent
    part (feeds/counts) is cached by _sidebar_data; only the active view/feed
    highlighting is layered on per request."""
    data = await _sidebar_data()
    return {
        **data,
        "view": active_view,
        "view_title": VIEW_TITLES.get(active_view, "Articles") if active_view else "Feed",
        "feed_id": active_feed_id,
    }


async def _build_sidebar_data() -> dict:
    """The expensive, view-independent part of the sidebar (feeds-by-tier, counts).
    Hits Miniflux + DB; cached by _sidebar_data()."""
    now = datetime.now(timezone.utc)
    feeds, counters, starred_data = await asyncio.gather(
        miniflux_client.get_feeds(),
        miniflux_client.get_feed_counters(),
        miniflux_client.get_entries(starred=True, limit=1),
    )
    unreads = counters.get("unreads", {}) or {}
    reads = counters.get("reads", {}) or {}

    async with get_conn() as conn:
        configs = await _feed_configs_full(conn)
        changed_ids = await _entries_with_changes(conn)

    for f in feeds:
        cfg = configs.get(f["id"], {})
        f["_priority"] = cfg.get("priority", 2)
        f["_proxy"] = bool(cfg.get("fetch_full_content"))
        f["_health"] = _feed_health_state(f, now)
        f["_unread"] = unreads.get(str(f["id"]), 0)

    groups = [
        {"key": p, "label": label,
         "items": sorted([f for f in feeds if f["_priority"] == p],
                         key=lambda f: f.get("title", "").lower())}
        for p, label in TIERS
    ]
    groups = [g for g in groups if g["items"]]

    unread_total = sum(int(v) for v in unreads.values())
    all_total = unread_total + sum(int(v) for v in reads.values())
    counts = {
        "unread": unread_total,
        "all": all_total,
        "read": sum(int(v) for v in reads.values()),
        "starred": starred_data.get("total", 0),
        "changed": len(changed_ids),
    }

    return {"groups": groups, "counts": counts}


async def _feed_configs_full(conn) -> dict[int, dict]:
    cur = await conn.execute(
        "SELECT feed_id, fetch_full_content, priority FROM feed_config"
    )
    return {row["feed_id"]: row for row in await cur.fetchall()}


def _wants_list_fragment(request: Request) -> bool:
    """True when HTMX is swapping just the list pane (sidebar reloads out-of-band).

    Requires an *explicit* HX-Target of "list-col". A header-less HX request (which
    can arise on some restore paths) must fall through to the full styled shell —
    never a bare, style-less fragment that would render unstyled if it ever landed
    at the document level.
    """
    return request.headers.get("HX-Request") == "true" and \
        request.headers.get("HX-Target") == "list-col"


@router.get("/entries", response_class=HTMLResponse)
async def entry_list(
    request: Request,
    feed_id: int | None = None,
    view: str | None = None,
    status: str | None = None,
    offset: int = 0,
    search: str | None = None,
    starred: bool = False,
    time_filter: str | None = None,
    changed: bool = False,
):
    limit = 50
    now = datetime.now(timezone.utc)

    # Normalize the smart-view selector (sidebar uses ?view=…; legacy params still work).
    if view == "starred":
        starred = True
    elif view == "changed":
        changed = True
    elif view == "all":
        status = "all"
    elif view == "read":
        status = "read"
    elif view and view.startswith("t:"):
        time_filter = view[2:]
    elif view == "unread":
        status = "unread"

    show_all = status == "all"
    if show_all:
        status = None
    elif status is None and not starred:
        status = "unread"
    time_params = _time_filter_params(time_filter)

    feed = None
    if feed_id:
        feed = await miniflux_client.get_feed(feed_id)
        data = await miniflux_client.get_entries(
            feed_id=feed_id, status=status, limit=limit, offset=offset,
            search=search, starred=starred, **time_params,
        )
        entries = data.get("entries", [])
        total = data.get("total", 0)
    else:
        # Over-fetch only when client-side "changed" filtering will prune the set
        # below; otherwise fetch just the page we render.
        fetch_limit = 200 if changed else limit
        data = await miniflux_client.get_entries(
            status=status, limit=fetch_limit, offset=offset,
            search=search, starred=starred, **time_params,
        )
        entries = data.get("entries", [])
        total = data.get("total", 0)

    async with get_conn() as conn:
        await _enrich_entries(conn, entries, now)

    if changed:
        entries = [e for e in entries if e["_has_changes"]]

    # Priority tier first (must-read feeds bubble up), then newest within tier.
    entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    entries.sort(key=lambda e: e.get("_priority", 2))
    if not feed_id:
        entries = entries[:limit]

    active_view = _active_view(
        feed_id=feed_id, view=view, starred=starred, changed=changed,
        time_filter=time_filter, show_all=show_all,
    )
    sidebar = await build_sidebar(active_view=active_view, active_feed_id=feed_id)

    ctx = {
        "entries": entries,
        "feed": feed,
        "feed_id": feed_id,
        "view": active_view,
        "status": status,
        "show_all": show_all,
        "offset": offset,
        "limit": limit,
        "total": total,
        "search": search or "",
        "starred": starred,
        "time_filter": time_filter or "",
        "changed": changed,
        "title": (feed.get("title") if feed else VIEW_TITLES.get(active_view, "Articles")),
        "sidebar": sidebar,
    }
    template = "entries_fragment.html" if _wants_list_fragment(request) else "entries.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("/entries/{entry_id}", response_class=HTMLResponse)
async def entry_detail(request: Request, entry_id: int):
    entry = await miniflux_client.get_entry(entry_id)
    if entry.get("status") == "unread":
        await miniflux_client.update_entry_status([entry_id], "read")
        entry["status"] = "read"
        _invalidate_sidebar_cache()  # unread→read flips counts

    feed_id = entry.get("feed_id")
    pub = entry.get("published_at", "")
    pub_ts = None
    if pub and feed_id:
        try:
            pub_ts = str(int(datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()))
        except Exception:
            pub_ts = None

    # The reader body needs the snapshot to paint; prev/next nav is independent, so
    # run them concurrently. Each get_conn() opens its own connection (db.py).
    async def _snapshot_and_count():
        async with get_conn() as conn:
            snap = await _get_snapshot(conn, entry_id)
            v = await _version_count(conn, entry_id) if snap else 0
        return snap, v

    async def _adjacent(direction: str):
        if not pub_ts:
            return None
        kwargs = ({"after": pub_ts, "direction": "asc"} if direction == "prev"
                  else {"before": pub_ts, "direction": "desc"})
        data = await miniflux_client.get_entries(feed_id=feed_id, limit=1, **kwargs)
        ents = data.get("entries") or []
        return ents[0]["id"] if ents else None

    (snapshot, vc), prev_entry_id, next_entry_id = await asyncio.gather(
        _snapshot_and_count(),
        _adjacent("prev"),
        _adjacent("next"),
    )

    # Detect audio enclosures for podcast player
    enclosures = entry.get("enclosures") or []
    audio_enclosure = next(
        (e for e in enclosures if (e.get("mime_type") or "").startswith("audio/")),
        None,
    )

    content_block = _content_block_ctx(
        entry_id, snapshot, vc, rss_html=(entry.get("content") or ""),
    )
    ctx = {
        "entry": entry,
        "snapshot": snapshot,
        "version_count": vc,
        "audio_enclosure": audio_enclosure,
        "prev_entry_id": prev_entry_id,
        "next_entry_id": next_entry_id,
        "content_block": content_block,
        "title": entry.get("title", "Article"),
    }
    # HTMX row-click loads only the reader pane; a direct visit renders the full shell.
    if request.headers.get("HX-Target") == "reader-col":
        return templates.TemplateResponse(request, "_reader.html", ctx)
    ctx["sidebar"] = await build_sidebar(
        active_view=None, active_feed_id=entry.get("feed_id"),
    )
    return templates.TemplateResponse(request, "entry.html", ctx)


def _content_block_ctx(entry_id: int, snapshot: dict | None, version_count: int,
                       message: str | None = None, rss_html: str = "") -> dict:
    """Build the `cb` context for _content_block.html (extraction bar + body)."""
    if snapshot:
        meta = snapshot.get("metadata") or {}
        return {
            "entry_id": entry_id,
            "has_full": True,
            "version": snapshot.get("version"),
            "fetched": snapshot["fetched_at"].strftime("%Y-%m-%d %H:%M") if snapshot.get("fetched_at") else "unknown",
            "version_count": version_count,
            "source": meta.get("source"),
            "message": message,
            "body_html": snapshot.get("content_html") or snapshot.get("content_text") or "",
        }
    return {"entry_id": entry_id, "has_full": False, "body_html": rss_html, "message": message}


def _render_content_block(entry_id: int, snapshot: dict, version_count: int, message: str | None = None) -> str:
    """Render the extraction bar + article HTML for the #entry-content swap."""
    cb = _content_block_ctx(entry_id, snapshot, version_count, message)
    return templates.env.get_template("_content_block.html").render(cb=cb)


@router.post("/entries/{entry_id}/fetch-full")
async def fetch_full_content(entry_id: int):
    """On-demand fetch of full article content, creating a new version if content changed."""
    entry = await miniflux_client.get_entry(entry_id)
    url = entry.get("url", "")
    if not url:
        return HTMLResponse('<span class="text-danger text-detail">No URL for entry</span>')

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT extract_rules FROM feed_config WHERE feed_id = %s",
            (entry.get("feed_id"),),
        )
        row = await cur.fetchone()
        extract_rules = (row["extract_rules"] if row else None) or {}

    cookies = await get_cookies_for_url(url)
    extracted = await fetch_and_extract(url, extract_rules, cookies=cookies)
    if not extracted:
        return HTMLResponse('<span class="text-danger text-detail">Extraction failed — no content found</span>')

    import hashlib
    import psycopg.types.json

    source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()

    async with get_conn() as conn:
        latest = await _get_snapshot(conn, entry_id)
        if latest and latest["content_hash"] == extracted["content_hash"]:
            vc = await _version_count(conn, entry_id)
            return HTMLResponse(_render_content_block(entry_id, latest, vc, "No changes detected"))

        next_version = (latest["version"] + 1) if latest else 1
        await conn.execute(
            """
            INSERT INTO article_snapshots
                (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version, source_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                entry_id,
                entry["feed_id"],
                url,
                extracted["content_text"],
                extracted["content_html"],
                extracted["content_hash"],
                psycopg.types.json.Json(extracted["metadata"]),
                next_version,
                source_hash,
            ),
        )
        await conn.commit()
        snapshot = await _get_snapshot(conn, entry_id)
        vc = await _version_count(conn, entry_id)

    return HTMLResponse(_render_content_block(entry_id, snapshot, vc))


def _structured_diff_lines(prev_text: str, curr_text: str) -> list[dict]:
    """unified_diff output → ``[{t, s}]`` with file/hunk headers stripped.

    ``t`` is one of ``" "`` (context), ``"+"`` (added), ``"-"`` (removed);
    ``s`` is the line text without the marker or trailing newline.
    """
    lines: list[dict] = []
    for raw in difflib.unified_diff(
        (prev_text or "").splitlines(),
        (curr_text or "").splitlines(),
        lineterm="",
    ):
        if raw[:3] in ("---", "+++") or raw.startswith("@@") or raw.startswith("\\"):
            continue
        t = raw[0] if raw and raw[0] in " +-" else " "
        lines.append({"t": t, "s": raw[1:]})
    return lines


def to_split_rows(lines: list[dict]) -> list[dict]:
    """Pair a unified line list into side-by-side rows for the split view.

    Consecutive removed/added lines pair by index (``left``/``right`` go ``None``
    when one side runs out → a ``.nil`` filler); context lines mirror to both sides.
    """
    rows: list[dict] = []
    dels: list[str] = []
    adds: list[str] = []

    def flush() -> None:
        for i in range(max(len(dels), len(adds))):
            rows.append({
                "left": dels[i] if i < len(dels) else None,
                "right": adds[i] if i < len(adds) else None,
                "ctx": False,
            })
        dels.clear()
        adds.clear()

    for line in lines:
        if line["t"] == "-":
            dels.append(line["s"])
        elif line["t"] == "+":
            adds.append(line["s"])
        else:
            flush()
            rows.append({"left": line["s"], "right": line["s"], "ctx": True})
    flush()
    return rows


@router.get("/entries/{entry_id}/diff", response_class=HTMLResponse)
async def entry_diff(request: Request, entry_id: int):
    """Content changes across snapshot versions, rendered as an overlay."""
    entry = await miniflux_client.get_entry(entry_id)
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT version, fetched_at, content_text FROM article_snapshots "
            "WHERE entry_id = %s ORDER BY version",
            (entry_id,),
        )
        snapshots = await cur.fetchall()

    sets = []
    for prev, curr in zip(snapshots, snapshots[1:]):
        lines = _structured_diff_lines(prev["content_text"], curr["content_text"])
        sets.append({
            "from": prev["version"],
            "to": curr["version"],
            "when": curr["fetched_at"].strftime("%Y-%m-%d %H:%M") if curr["fetched_at"] else "",
            "lines": lines,
            "split_rows": to_split_rows(lines),
        })

    ctx = {"entry": entry, "sets": sets, "version_count": len(snapshots)}
    template = "_diff_overlay.html" if request.headers.get("HX-Request") else "diff.html"
    return templates.TemplateResponse(request, template, ctx)


@router.post("/entries/{entry_id}/mark-read")
async def mark_read(entry_id: int):
    await miniflux_client.update_entry_status([entry_id], "read")
    _invalidate_sidebar_cache()
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/mark-unread" hx-swap="outerHTML">Mark unread</button>'
    )


@router.post("/entries/{entry_id}/mark-unread")
async def mark_unread(entry_id: int):
    await miniflux_client.update_entry_status([entry_id], "unread")
    _invalidate_sidebar_cache()
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/mark-read" hx-swap="outerHTML">Mark read</button>'
    )


@router.post("/entries/{entry_id}/toggle-star")
async def toggle_star(entry_id: int):
    await miniflux_client.toggle_bookmark(entry_id)
    _invalidate_sidebar_cache()
    # Miniflux toggles, so we re-fetch to get current state
    entry = await miniflux_client.get_entry(entry_id)
    starred = entry.get("starred", False)
    cls = "starred" if starred else ""
    label = "&#9733; Starred" if starred else "&#9734; Star"
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/toggle-star" hx-swap="outerHTML" class="star-btn {cls}">{label}</button>'
    )


@router.post("/entries/mark-all-read")
async def mark_all_read(request: Request):
    """Mark all visible entries as read. Accepts JSON body with entry_ids."""
    body = await request.json()
    entry_ids = body.get("entry_ids", [])
    if entry_ids:
        await miniflux_client.update_entry_status(entry_ids, "read")
        _invalidate_sidebar_cache()
    return JSONResponse({"ok": True, "count": len(entry_ids)})


@router.get("/api/new-count")
async def new_count(since: int = 0):
    """Return count of unread entries."""
    data = await miniflux_client.get_entries(status="unread", limit=1)
    return JSONResponse({"count": data.get("total", 0)})
