import asyncio
import difflib
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from lxml import html as lxml_html
from psycopg.types.json import Jsonb

from app import browser_login, db, embeddings, lenses, miniflux_client, ranker, ranker_client
from app.config import EMBED_ENABLED, MUTE_SIGNALS_ENABLED
from app.db import get_conn
from app.extractor import fetch_and_extract
from app.feed_health import classify
from app.routes.cookies import (
    cookie_meta_for_domain,
    domain_from_url,
    get_cookies_for_url,
)
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


DEFAULT_FEED_PREFS = {"show_read_default": False, "author_mutes": [], "tag_mutes": []}


async def _feed_prefs(conn, feed_id: int) -> dict:
    """Per-feed reading prefs: show-read default + author/tag mute lists (Part A)."""
    cur = await conn.execute(
        "SELECT show_read_default, author_mutes, tag_mutes "
        "FROM feed_config WHERE feed_id = %s",
        (feed_id,),
    )
    row = await cur.fetchone()
    if not row:
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in DEFAULT_FEED_PREFS.items()}
    return {
        "show_read_default": bool(row["show_read_default"]),
        "author_mutes": list(row["author_mutes"] or []),
        "tag_mutes": list(row["tag_mutes"] or []),
    }


async def _all_feed_mutes(conn) -> dict[int, dict]:
    """feed_id -> {author_mutes, tag_mutes} for every feed that has any mute.
    Used to down-rank muted authors/tags in cross-feed views."""
    cur = await conn.execute(
        "SELECT feed_id, author_mutes, tag_mutes FROM feed_config "
        "WHERE author_mutes <> '[]'::jsonb OR tag_mutes <> '[]'::jsonb"
    )
    return {
        row["feed_id"]: {
            "author_mutes": list(row["author_mutes"] or []),
            "tag_mutes": list(row["tag_mutes"] or []),
        }
        for row in await cur.fetchall()
    }


def _entry_tag_names(entry: dict) -> list[str]:
    """Miniflux entry tags are strings, but tolerate {title|name} objects too."""
    out = []
    for t in entry.get("tags") or []:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            name = t.get("title") or t.get("name")
            if name:
                out.append(name)
    return out


def _is_muted(entry: dict, author_mutes, tag_mutes) -> bool:
    author = (entry.get("author") or "").strip().casefold()
    if author and author in {a.strip().casefold() for a in author_mutes}:
        return True
    if tag_mutes:
        tset = {t.casefold() for t in tag_mutes}
        if any(t.casefold() in tset for t in _entry_tag_names(entry)):
            return True
    return False


def _apply_mutes(entries: list[dict], author_mutes, tag_mutes) -> list[dict]:
    if not author_mutes and not tag_mutes:
        return entries
    return [e for e in entries if not _is_muted(e, author_mutes, tag_mutes)]


async def _upsert_feed_config(conn, feed_id: int, col: str, val) -> None:
    """Upsert a single feed_config column. `col` is a fixed internal name, never
    user input, so interpolation is safe."""
    cur = await conn.execute("SELECT 1 FROM feed_config WHERE feed_id = %s", (feed_id,))
    if await cur.fetchone():
        await conn.execute(
            f"UPDATE feed_config SET {col} = %s, updated_at = NOW() WHERE feed_id = %s",
            (val, feed_id),
        )
    else:
        await conn.execute(
            f"INSERT INTO feed_config (feed_id, {col}) VALUES (%s, %s)",
            (feed_id, val),
        )


# Quality-of-attention signals for the learning ranker (Part B). Deliberately
# excludes plain reads — see the engagement_events table comment.
async def _log_engagement(entry_id: int | None, signal: str, value: float = 1.0,
                          feed_id: int | None = None, detail: str | None = None) -> None:
    """Best-effort insert of an engagement event. Never raises into the request.
    entry_id is optional: mute/unmute events describe a feed-level config action,
    not an article, and carry the muted author/tag in `detail` instead."""
    try:
        async with get_conn() as conn:
            await conn.execute(
                "INSERT INTO engagement_events (entry_id, feed_id, signal, value, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (entry_id, feed_id, signal, value, detail),
            )
            await conn.commit()
    except Exception:
        logger.exception("failed to log engagement %s for entry %s", signal, entry_id)


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
    now = datetime.now(UTC)
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
    now = datetime.now(UTC)
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
        f["_health"] = classify(f, now).state
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


def _smart_eligible(*, starred: bool, changed: bool, search: str | None,
                    status: str | None) -> bool:
    """Learned ranking applies to plain cross-feed lists: Unread/All and the
    time-window views. Starred/changed/search stay chronological — those are
    lookup surfaces, not triage surfaces."""
    return not starred and not changed and not search and status in (None, "unread")


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
    order: str | None = None,
):
    limit = 50
    now = datetime.now(UTC)

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

    # Per-feed reading prefs drive the default show-read state on a feed page.
    feed_prefs = dict(DEFAULT_FEED_PREFS)
    if feed_id:
        async with get_conn() as conn:
            feed_prefs = await _feed_prefs(conn, feed_id)
        if status is None and view is None and not starred and not changed:
            status = "all" if feed_prefs["show_read_default"] else "unread"

    show_all = status == "all"
    if show_all:
        status = None
    elif status is None and not starred:
        status = "unread"
    time_params = _time_filter_params(time_filter)
    lens = lenses.normalize(order, request.cookies.get("lens"))

    smart_eligible = use_scores = ranked = False
    ranker_signals = 0   # quality-signal count, surfaced as the cross-feed warmth meter

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
        # The learned ranker applies to plain cross-feed lists: Unread/All and the
        # time-window views (Starred/Changed/Search stay chronological).
        smart_eligible = _smart_eligible(
            starred=starred, changed=changed, search=search, status=status
        )
        use_scores = smart_eligible and lens in ("smart", "catchup", "dive")
        # Over-fetch a candidate pool when ranking (so the ranker can promote an
        # older high-affinity item) or when client-side "changed" pruning will cut it.
        fetch_limit = 200 if (changed or use_scores) else limit
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

    if feed_id:
        # Feed page: plain reverse-chronological, with this feed's mutes hard-applied.
        entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
        entries = _apply_mutes(
            entries, feed_prefs["author_mutes"], feed_prefs["tag_mutes"]
        )
    else:
        # Muted authors/tags (per their own feed) are always demoted to the bottom —
        # the "down-rank cross-feed" half of the per-feed mute.
        async with get_conn() as conn:
            feed_mutes = await _all_feed_mutes(conn)
            if smart_eligible:
                cur = await conn.execute("SELECT count(*) AS n FROM engagement_events")
                row = await cur.fetchone()
                ranker_signals = int(row["n"]) if row else 0
        for e in entries:
            m = feed_mutes.get(e.get("feed_id"))
            e["_muted"] = bool(m and _is_muted(e, m["author_mutes"], m["tag_mutes"]))

        scores = None
        sims: dict[int, float] = {}
        if use_scores:
            priorities = {e.get("feed_id"): e.get("_priority", 2) for e in entries}
            async with get_conn() as conn:
                sims = await embeddings.embed_sims(conn, [e["id"] for e in entries])
            scores = await ranker_client.score(
                ranker.build_articles(entries, priorities, now, sims)
            )
        entries, ranked = lenses.order_entries(lens, entries, scores, sims, now)
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
        "feed_prefs": feed_prefs,
        "show_read": feed_prefs["show_read_default"] if feed_id else None,
        "smart_eligible": smart_eligible,
        "order": lens if smart_eligible else None,
        "ranked": ranked,
        "ranker_signals": ranker_signals,
    }
    template = "entries_fragment.html" if _wants_list_fragment(request) else "entries.html"
    resp = templates.TemplateResponse(request, template, ctx)
    if order in lenses.LENSES:   # explicit choice → persist for future navigations
        resp.set_cookie("lens", order, max_age=31536000, path="/", samesite="lax")
    return resp


async def _render_after_pref_change(request: Request, feed_id: int):
    """Re-render whichever surface triggered a per-feed pref change: the list pane
    (chips on the feed page / reader) or the feed-settings overlay."""
    if request.headers.get("HX-Target") == "overlay-slot":
        from app.routes import feeds as feeds_routes
        return await feeds_routes.feed_settings(request, feed_id)
    return await entry_list(request, feed_id=feed_id)


@router.post("/feeds/{feed_id}/show-read", response_class=HTMLResponse)
async def toggle_show_read(request: Request, feed_id: int):
    """Flip whether read articles show on this feed's page, persist as its default,
    and re-render the triggering surface."""
    async with get_conn() as conn:
        prefs = await _feed_prefs(conn, feed_id)
        await _upsert_feed_config(
            conn, feed_id, "show_read_default", not prefs["show_read_default"]
        )
        await conn.commit()
    return await _render_after_pref_change(request, feed_id)


@router.post("/feeds/{feed_id}/mute", response_class=HTMLResponse)
async def add_mute(request: Request, feed_id: int,
                   kind: str = Form(...), value: str = Form(...),
                   entry_id: int | None = Form(None)):
    """Hide an author or tag on this feed (and down-rank it cross-feed)."""
    if kind not in ("author", "tag"):
        return JSONResponse({"ok": False, "error": "bad kind"}, status_code=400)
    col = "author_mutes" if kind == "author" else "tag_mutes"
    value = value.strip()
    async with get_conn() as conn:
        prefs = await _feed_prefs(conn, feed_id)
        cur_list = prefs["author_mutes" if kind == "author" else "tag_mutes"]
        if value and value not in cur_list:
            cur_list.append(value)
        await _upsert_feed_config(conn, feed_id, col, Jsonb(cur_list))
        await conn.commit()
    _invalidate_sidebar_cache()
    if MUTE_SIGNALS_ENABLED and value:
        # entry_id is provenance only (which article the mute was clicked from) —
        # the observation itself is about the author/tag, never that article.
        await _log_engagement(entry_id, f"mute_{kind}", 1.0, feed_id, detail=value)
    return await _render_after_pref_change(request, feed_id)


@router.post("/feeds/{feed_id}/unmute", response_class=HTMLResponse)
async def remove_mute(request: Request, feed_id: int,
                      kind: str = Form(...), value: str = Form(...)):
    if kind not in ("author", "tag"):
        return JSONResponse({"ok": False, "error": "bad kind"}, status_code=400)
    col = "author_mutes" if kind == "author" else "tag_mutes"
    async with get_conn() as conn:
        prefs = await _feed_prefs(conn, feed_id)
        cur_list = [v for v in prefs["author_mutes" if kind == "author" else "tag_mutes"]
                    if v != value]
        await _upsert_feed_config(conn, feed_id, col, Jsonb(cur_list))
        await conn.commit()
    _invalidate_sidebar_cache()
    if MUTE_SIGNALS_ENABLED and value:
        await _log_engagement(None, f"unmute_{kind}", 1.0, feed_id, detail=value)
    return await _render_after_pref_change(request, feed_id)


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
        feed_id=entry.get("feed_id"),
        show_login=await _show_login_affordance(entry.get("url")),
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
        "related_enabled": EMBED_ENABLED and db.VECTOR_READY,
    }
    # HTMX row-click loads only the reader pane; a direct visit renders the full shell.
    if request.headers.get("HX-Target") == "reader-col":
        return templates.TemplateResponse(request, "_reader.html", ctx)
    ctx["sidebar"] = await build_sidebar(
        active_view=None, active_feed_id=entry.get("feed_id"),
    )
    return templates.TemplateResponse(request, "entry.html", ctx)


async def _show_login_affordance(url: str | None) -> bool:
    """Whether to offer a "Subscription login" link for this article's site.

    True for domains with a configured login recipe, or ones we already hold a
    session for — so the link appears where login is meaningful, not on every
    ordinary feed.
    """
    domain = domain_from_url(url)
    if not domain:
        return False
    if browser_login.has_login_recipe(domain):
        return True
    return bool(await cookie_meta_for_domain(domain))


def _snapshot_has_content(snapshot: dict | None) -> bool:
    """True if a snapshot holds real article content — prose *or* media — rather
    than an empty SPA shell.

    Early paywalled-SPA fetches (pre browser-render tier) stored a 0-char shell.
    We don't hide those — the reader shows the (empty) full state with a toggle to
    the original RSS body — but this flags them so it can be labelled. A text-free
    photo/comic/video post is NOT empty: its content is the img/embed, so media
    counts (a YouTube embed renders as a click-through "Watch" link).
    """
    if not snapshot:
        return False
    if (snapshot.get("content_text") or "").strip():
        return True
    html = snapshot.get("content_html") or ""
    if not html:
        return False
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return False
    if tree.text_content().strip():
        return True
    return bool(tree.xpath(
        "//img[@src] | //iframe[@src] | //video[@src] | //audio[@src] | //source[@src]"
    ))


def _content_block_ctx(entry_id: int, snapshot: dict | None, version_count: int,
                       message: str | None = None, rss_html: str = "",
                       feed_id: int | None = None, show_login: bool = False) -> dict:
    """Build the `cb` context for _content_block.html (extraction bar + body).

    When a snapshot exists we always show the full-fetch result (even if empty)
    with a toggle to the original RSS body — never silently substitute one for
    the other, so it's clear what the fetch actually produced.
    """
    base = {"entry_id": entry_id, "feed_id": feed_id, "show_login": show_login,
            "rss_html": rss_html, "message": message}
    if snapshot:
        meta = snapshot.get("metadata") or {}
        return {
            **base,
            "has_full": True,
            "full_empty": not _snapshot_has_content(snapshot),
            "version": snapshot.get("version"),
            "fetched": snapshot["fetched_at"].strftime("%Y-%m-%d %H:%M") if snapshot.get("fetched_at") else "unknown",
            "version_count": version_count,
            "source": meta.get("source"),
            "body_html": snapshot.get("content_html") or snapshot.get("content_text") or "",
        }
    return {**base, "has_full": False, "full_empty": False, "body_html": rss_html}


async def _render_content_block(entry_id: int, snapshot: dict, version_count: int,
                                message: str | None = None, feed_id: int | None = None,
                                url: str | None = None, rss_html: str = "") -> str:
    """Render the extraction bar + article HTML for the #entry-content swap."""
    cb = _content_block_ctx(entry_id, snapshot, version_count, message, rss_html=rss_html,
                            feed_id=feed_id, show_login=await _show_login_affordance(url))
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
            return HTMLResponse(await _render_content_block(entry_id, latest, vc, "No changes detected", feed_id=entry.get("feed_id"), url=url, rss_html=entry.get("content") or ""))

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

    return HTMLResponse(await _render_content_block(entry_id, snapshot, vc, feed_id=entry.get("feed_id"), url=url, rss_html=entry.get("content") or ""))


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
    # strict=False: pairing each snapshot with its successor is a sliding window —
    # the operands are deliberately unequal in length.
    for prev, curr in zip(snapshots, snapshots[1:], strict=False):
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
    # Starring is a strong interest signal; un-starring a (mild) reversal.
    await _log_engagement(
        entry_id, "star" if starred else "unstar", 1.0, entry.get("feed_id")
    )
    cls = "starred" if starred else ""
    label = "&#9733; Starred" if starred else "&#9734; Star"
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/toggle-star" hx-swap="outerHTML" class="star-btn {cls}">{label}</button>'
    )


# Signals reported directly by the client. Star is logged in toggle_star;
# thumbs go through /thumb. Dwell below the threshold is dropped (a swipe-past,
# not a deliberate read).
_CLIENT_SIGNALS = {"open_original", "dwell"}
_DWELL_MIN_SECONDS = 4.0
_DWELL_MAX_SECONDS = 3600.0


def _coerce_feed_id(raw) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/entries/{entry_id}/event")
async def log_event(entry_id: int, request: Request):
    """Record a client-reported engagement signal (open-original, dwell)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    signal = (body.get("signal") or "").strip()
    if signal not in _CLIENT_SIGNALS:
        return JSONResponse({"ok": False, "error": "bad signal"}, status_code=400)
    try:
        value = float(body.get("value")) if body.get("value") is not None else 1.0
    except (TypeError, ValueError):
        value = 1.0
    if signal == "dwell":
        if value < _DWELL_MIN_SECONDS:
            return JSONResponse({"ok": True, "skipped": True})  # swipe-past, not interest
        value = min(value, _DWELL_MAX_SECONDS)
    await _log_engagement(entry_id, signal, value, _coerce_feed_id(body.get("feed_id")))
    return JSONResponse({"ok": True})


@router.post("/entries/{entry_id}/thumb")
async def thumb(entry_id: int, request: Request):
    """Explicit 'more/less like this' feedback."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    direction = (body.get("dir") or "").strip()
    if direction not in ("up", "down"):
        return JSONResponse({"ok": False, "error": "bad direction"}, status_code=400)
    await _log_engagement(
        entry_id, "thumb_" + direction, 1.0 if direction == "up" else -1.0,
        _coerce_feed_id(body.get("feed_id")),
    )
    return JSONResponse({"ok": True})


@router.get("/entries/{entry_id}/why", response_class=HTMLResponse)
async def why_ranked(entry_id: int, request: Request):
    """The 'why this ranked' explanation fragment — the largest ± per-feature
    contributions to this entry's learned score. Loaded lazily when the row's why
    chip is expanded (cross-feed Smart views only)."""
    entry = await miniflux_client.get_entry(entry_id)
    feed = entry.get("feed") or {}
    feed_id = entry.get("feed_id") or feed.get("id")
    prio = 2
    if feed_id:
        async with get_conn() as conn:
            cur = await conn.execute(
                "SELECT priority FROM feed_config WHERE feed_id = %s", (feed_id,))
            row = await cur.fetchone()
        if row and row["priority"] is not None:
            prio = row["priority"]
    now = datetime.now(UTC)
    async with get_conn() as conn:
        sims = await embeddings.embed_sims(conn, [entry_id])
    article = {"entry_id": entry_id,
               "features": ranker.entry_features(entry, prio, now, sims.get(entry_id))}
    reasons = await ranker_client.explain(article)
    items = [
        {"label": ranker.feature_label(r["name"], feed.get("title")), "dir": r["dir"]}
        for r in (reasons or [])
    ]
    return templates.TemplateResponse(request, "_why.html", {"items": items})


@router.get("/entries/{entry_id}/related", response_class=HTMLResponse)
async def related(entry_id: int, request: Request):
    """'Related' — nearest neighbours of this entry by embedding cosine, loaded
    lazily below the article body. Renders nothing at all when embeddings are off,
    the entry has no vector yet, or nothing clears the similarity floor."""
    if not (EMBED_ENABLED and db.VECTOR_READY):
        return HTMLResponse("")
    try:
        async with get_conn() as conn:
            candidates, target_title = await embeddings.related_candidates(conn, entry_id)
        picks = embeddings.pick_related(candidates, target_title)
        if not picks:
            return HTMLResponse("")
        feeds = {f["id"]: f.get("title") for f in await miniflux_client.get_feeds()}
        items = [
            {
                "id": p["entry_id"],
                "title": p.get("title") or "(untitled)",
                "feed_title": feeds.get(p.get("feed_id")) or "",
                "published_at": p.get("published_at"),
            }
            for p in picks
        ]
    except Exception as exc:  # a related list is a nicety — never break the reader
        logger.debug("related failed open: %s", exc)
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_related.html", {"items": items})


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
