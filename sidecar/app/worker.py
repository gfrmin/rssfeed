import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from app import browser_login, credvault, miniflux_client
from app.config import (
    WORKER_BACKFILL_MAX_AGE_DAYS,
    WORKER_EXTRACT_BACKOFF_BASE_MIN,
    WORKER_EXTRACT_BACKOFF_MAX_MIN,
    WORKER_EXTRACT_BATCH,
    WORKER_EXTRACT_MAX_ATTEMPTS,
    WORKER_EXTRACT_RETRY_BATCH,
    WORKER_POLL_INTERVAL,
)
from app.db import get_conn
from app.extractor import fetch_and_extract
from app.routes.cookies import (
    cookie_meta_for_domain,
    domain_from_url,
    get_cookies_for_url,
    upsert_cookies,
)

logger = logging.getLogger(__name__)

# Auto re-login: refresh a domain's paywall session when its cookies are missing
# or older than this, but no more than once per cooldown (browser logins are slow
# and metered). Throttle state is per-process and intentionally not persisted.
_RELOGIN_STALE_AFTER = timedelta(days=25)
_RELOGIN_COOLDOWN_S = 3 * 3600
_last_login_attempt: dict[str, float] = {}

# Feeds whose feed_config row outlived their Miniflux feed (deleted upstream).
# Miniflux answers /v1/feeds/<id>/entries with 400 "invalid feed ID" (or 404),
# which otherwise spams an ERROR every poll. We skip them for the rest of the
# process — non-destructively: the local feed_config and any collected snapshots
# are left untouched (a feed can be re-subscribed and resume). Cleared on restart.
_missing_feeds: set[int] = set()


def _feed_gone_from_miniflux(exc: Exception) -> bool:
    """True if ``exc`` is Miniflux reporting that a feed no longer exists."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 404)


_BACKFILL_MAX_AGE = (
    timedelta(days=WORKER_BACKFILL_MAX_AGE_DAYS) if WORKER_BACKFILL_MAX_AGE_DAYS > 0 else None
)


def _too_old_to_backfill(entry: dict) -> bool:
    """True if a never-fetched entry was ALREADY old, at the moment this decision
    is made, per Miniflux's own ``created_at`` (when *we* first became aware of
    it — fixed at insertion, never mutated) rather than the ever-growing
    ``now() - published_at``.

    Called at most once per entry, by _process_feed_cursor the first (and only)
    time its forward-only cursor reaches that entry_id — never re-evaluated on a
    later poll. That discipline, not just the field swap, is what avoids
    silently abandoning an entry that was fresh when first seen but simply took
    a while to succeed (previously: once it crossed the age line on some later
    poll, it was skipped forever with no warning).
    """
    if _BACKFILL_MAX_AGE is None:
        return False
    created = entry.get("created_at")
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except Exception:
        return False
    return datetime.now(UTC) - dt > _BACKFILL_MAX_AGE


def _next_attempt(
    count: int,
    max_attempts: int = WORKER_EXTRACT_MAX_ATTEMPTS,
    base_min: int = WORKER_EXTRACT_BACKOFF_BASE_MIN,
    cap_min: int = WORKER_EXTRACT_BACKOFF_MAX_MIN,
) -> tuple[bool, timedelta | None]:
    """Pure backoff decision for an entry that has now failed `count` times
    (the attempt_count *after* this failure, not before).

    Returns (given_up, retry_delay); retry_delay is None once given_up.
    Exponential backoff (base_min * 2**(count-1)), capped at cap_min.
    """
    given_up = count >= max_attempts
    delay = None if given_up else timedelta(minutes=min(base_min * (2 ** (count - 1)), cap_min))
    return given_up, delay


async def ensure_fresh_login(domain: str | None) -> None:
    """Re-login a domain (from saved credentials) if its cookies are missing/stale.

    Fail-soft and throttled: any error leaves existing cookies untouched, and a
    domain is retried at most once per cooldown regardless of outcome.
    """
    if not domain or not browser_login.login_available():
        return
    now = time.monotonic()
    if now - _last_login_attempt.get(domain, 0.0) < _RELOGIN_COOLDOWN_S:
        return
    meta = await cookie_meta_for_domain(domain)
    if meta and meta["updated_at"] and (
        datetime.now(UTC) - meta["updated_at"] < _RELOGIN_STALE_AFTER
    ):
        return  # cookies still fresh
    creds = await credvault.get_credentials(domain)
    if not creds:
        return
    _last_login_attempt[domain] = now
    logger.info("Auto re-login for %s (cookies missing/stale)", domain)
    try:
        cookies = await browser_login.login_and_get_cookies(
            domain, creds["username"], creds["password"]
        )
    except Exception:
        logger.exception("Auto re-login crashed for %s", domain)
        return
    if cookies:
        await upsert_cookies(domain, cookies)
        logger.info("Auto re-login for %s refreshed %d cookies", domain, len(cookies))
    else:
        logger.warning("Auto re-login for %s failed (kept existing cookies)", domain)


async def _get_enabled_feeds(conn: psycopg.AsyncConnection) -> dict[int, dict]:
    """Return {feed_id: {extract_rules}} for feeds with full content enabled."""
    cur = await conn.execute(
        "SELECT feed_id, extract_rules FROM feed_config WHERE fetch_full_content = TRUE"
    )
    return {
        row["feed_id"]: {"extract_rules": row["extract_rules"] or {}}
        for row in await cur.fetchall()
    }


async def _get_snapshot_info(conn: psycopg.AsyncConnection, entry_id: int) -> tuple[bool, str | None, str | None, int]:
    """Return (exists, source_hash, content_hash, max_version) for the latest snapshot of an entry."""
    cur = await conn.execute(
        "SELECT source_hash, content_hash, version FROM article_snapshots WHERE entry_id = %s ORDER BY version DESC LIMIT 1",
        (entry_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False, None, None, 0
    return True, row["source_hash"], row["content_hash"], row["version"]


async def _store_snapshot(
    conn: psycopg.AsyncConnection,
    entry_id: int,
    feed_id: int,
    url: str,
    extracted: dict,
    source_hash: str,
    version: int = 1,
) -> None:
    await conn.execute(
        """
        INSERT INTO article_snapshots
            (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version, source_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        """,
        (
            entry_id,
            feed_id,
            url,
            extracted["content_text"],
            extracted["content_html"],
            extracted["content_hash"],
            psycopg.types.json.Json(extracted["metadata"]),
            version,
            source_hash,
        ),
    )
    await conn.commit()


async def _feed_cursor(conn: psycopg.AsyncConnection, feed_id: int) -> int:
    """Highest entry id this feed's discovery cursor has examined so far. Lazily
    created at 0 the first time a feed is processed (walks from the start of
    the feed's Miniflux history)."""
    cur = await conn.execute(
        "SELECT cursor_entry_id FROM extract_cursor WHERE feed_id = %s", (feed_id,)
    )
    row = await cur.fetchone()
    if row is None:
        await conn.execute(
            "INSERT INTO extract_cursor (feed_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (feed_id,),
        )
        await conn.commit()
        return 0
    return row["cursor_entry_id"]


async def _save_feed_cursor(conn: psycopg.AsyncConnection, feed_id: int, entry_id: int) -> None:
    await conn.execute(
        "UPDATE extract_cursor SET cursor_entry_id = GREATEST(cursor_entry_id, %s), "
        "updated_at = NOW() WHERE feed_id = %s",
        (entry_id, feed_id),
    )
    await conn.commit()


async def _has_attempt_record(conn: psycopg.AsyncConnection, entry_id: int) -> bool:
    cur = await conn.execute("SELECT 1 FROM extract_attempts WHERE entry_id = %s", (entry_id,))
    return await cur.fetchone() is not None


async def _record_attempt_failure(
    conn: psycopg.AsyncConnection, entry_id: int, feed_id: int, error: str
) -> None:
    """Upsert the durable attempt record; logs (WARNING) exactly once, at the
    moment an entry crosses WORKER_EXTRACT_MAX_ATTEMPTS — the give-up event the
    old age-gate never logged at all.

    The count increment (``attempt_count = attempt_count + 1``) is done in the
    same statement as the upsert and read back via RETURNING, rather than a
    separate SELECT-then-write — the live worker and the one-off backlog
    script (scripts/backfill_orphaned_extractions.py) can race on the same
    entry_id, and a read-then-write round trip would let both land on the same
    prior count and silently lose an increment.
    """
    cur = await conn.execute(
        """
        INSERT INTO extract_attempts (entry_id, feed_id, attempt_count, last_attempt_at, last_error, updated_at)
        VALUES (%s, %s, 1, NOW(), %s, NOW())
        ON CONFLICT (entry_id) DO UPDATE SET
            attempt_count = extract_attempts.attempt_count + 1,
            last_attempt_at = NOW(), last_error = EXCLUDED.last_error, updated_at = NOW()
        RETURNING attempt_count
        """,
        (entry_id, feed_id, error[:500]),
    )
    count = (await cur.fetchone())["attempt_count"]
    given_up, delay = _next_attempt(count)
    next_retry_at = None if given_up else datetime.now(UTC) + delay
    await conn.execute(
        "UPDATE extract_attempts SET next_retry_at = %s, given_up = %s WHERE entry_id = %s",
        (next_retry_at, given_up, entry_id),
    )
    if given_up:
        logger.warning(
            "Giving up on entry %d (feed %d) after %d failed extraction attempts: %s",
            entry_id, feed_id, count, error,
        )
    await conn.commit()


async def _attempt_extraction(
    conn: psycopg.AsyncConnection, entry: dict, feed_id: int, extract_rules: dict
) -> bool:
    """Try to extract+store one never-snapshotted entry, recording the outcome in
    extract_attempts (cleared on success — article_snapshots then becomes the
    durable record; incremented/backed-off/given-up on failure). Shared by the
    cursor discovery pass and the retry pass. Returns True iff a snapshot was
    stored."""
    entry_id = entry["id"]
    url = entry.get("url", "")
    if not url:
        return False
    source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()
    logger.info("Extracting entry %d: %s", entry_id, url)
    try:
        cookies = await get_cookies_for_url(url)
        extracted = await fetch_and_extract(url, extract_rules, cookies=cookies)
    except Exception:
        logger.exception("Extraction crashed for entry %d: %s", entry_id, url)
        extracted = None
    if extracted:
        try:
            await _store_snapshot(conn, entry_id, feed_id, url, extracted, source_hash, version=1)
        except psycopg.errors.UniqueViolation:
            # Another process (the live worker and the one-off backfill script
            # can run concurrently) already stored this exact snapshot first —
            # not an error, just a race we lost. Un-poison the connection and
            # treat it as resolved.
            await conn.rollback()
            logger.info("Entry %d already has this snapshot (concurrent extraction)", entry_id)
        await conn.execute("DELETE FROM extract_attempts WHERE entry_id = %s", (entry_id,))
        await conn.commit()
        return True
    logger.warning("Extraction failed for entry %d: %s", entry_id, url)
    await _record_attempt_failure(conn, entry_id, feed_id, "extraction failed (no content)")
    return False


async def _process_feed_cursor(
    conn: psycopg.AsyncConnection, feed_id: int, extract_rules: dict
) -> int:
    """Walk this feed's entries forward by ascending id from its saved cursor,
    WORKER_EXTRACT_BATCH at a time, extracting never-snapshotted entries.
    Every entry_id seen this pass advances the cursor unconditionally, so an
    entry can never fall out of consideration again purely because the feed
    published enough newer entries — the bug this replaces (a plain "top 50
    most recent" query that silently forgot a slow-to-succeed entry once ~50
    newer siblings had arrived).

    Because the cursor is forward-only, each entry_id is examined here at
    most once ever — so an entry that already has a snapshot is left alone
    (RSS-content-change re-checks for already-snapshotted entries are
    _process_feed_recency's job, which keeps re-examining the recent window
    indefinitely, the same way the pre-fix code always did).
    """
    cursor = await _feed_cursor(conn, feed_id)
    data = await miniflux_client.get_entries(
        feed_id=feed_id, limit=WORKER_EXTRACT_BATCH,
        order="id", direction="asc", after_entry_id=cursor,
    )
    entries = data.get("entries", [])
    if not entries:
        return 0

    # Refresh a stored paywall login before fetching this feed's articles.
    first_url = next((e.get("url") for e in entries if e.get("url")), None)
    if first_url:
        await ensure_fresh_login(domain_from_url(first_url))

    processed = 0
    reached = cursor
    for entry in entries:
        entry_id = entry["id"]
        reached = max(reached, entry_id)
        url = entry.get("url", "")
        if not url:
            continue

        exists, *_ = await _get_snapshot_info(conn, entry_id)
        if exists:
            continue  # already resolved — not this pass's concern

        if await _has_attempt_record(conn, entry_id):
            # Already attempted at least once elsewhere (e.g. the backfill
            # script) — its fate (backoff timing, given_up) is now owned
            # entirely by _process_retry_batch, not re-decided here.
            continue
        if _too_old_to_backfill(entry):
            logger.info(
                "Archive backfill skip: entry %d (feed %d) created_at=%s is older "
                "than WORKER_BACKFILL_MAX_AGE_DAYS=%d; will not be attempted",
                entry_id, feed_id, entry.get("created_at"), WORKER_BACKFILL_MAX_AGE_DAYS,
            )
            continue
        if await _attempt_extraction(conn, entry, feed_id, extract_rules):
            processed += 1

    await _save_feed_cursor(conn, feed_id, reached)
    return processed


async def _process_feed_recency(
    conn: psycopg.AsyncConnection, feed_id: int, extract_rules: dict
) -> int:
    """Watch each feed's most-recent WORKER_EXTRACT_BATCH entries (by publish
    date — the same window this worker always used) for RSS content changes
    on entries that already have a snapshot, re-fetching and versioning as
    before the fix. Never-snapshotted entries are _process_feed_cursor's job
    exclusively — skipped here so the two passes can't race to extract the
    same brand-new entry twice.
    """
    data = await miniflux_client.get_entries(feed_id=feed_id, limit=WORKER_EXTRACT_BATCH)
    entries = data.get("entries", [])
    if not entries:
        return 0

    first_url = next((e.get("url") for e in entries if e.get("url")), None)
    if first_url:
        await ensure_fresh_login(domain_from_url(first_url))

    processed = 0
    for entry in entries:
        entry_id = entry["id"]
        url = entry.get("url", "")
        if not url:
            continue

        exists, stored_hash, stored_content_hash, max_version = await _get_snapshot_info(conn, entry_id)
        if not exists:
            continue  # never-snapshotted entries are _process_feed_cursor's job

        source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()
        if stored_hash == source_hash:
            continue  # No change in RSS content

        if stored_hash is None:
            # Backfill source_hash for pre-existing snapshots (no re-fetch)
            await conn.execute(
                "UPDATE article_snapshots SET source_hash = %s "
                "WHERE entry_id = %s AND version = %s",
                (source_hash, entry_id, max_version),
            )
            await conn.commit()
            continue

        logger.info("RSS content changed for entry %d, re-fetching: %s", entry_id, url)
        try:
            cookies = await get_cookies_for_url(url)
            extracted = await fetch_and_extract(url, extract_rules, cookies=cookies)
        except Exception:
            logger.exception("Extraction crashed for entry %d: %s", entry_id, url)
            extracted = None
        if not extracted:
            # Update source_hash to prevent infinite retry on extraction failure
            await conn.execute(
                "UPDATE article_snapshots SET source_hash = %s "
                "WHERE entry_id = %s AND version = %s",
                (source_hash, entry_id, max_version),
            )
            await conn.commit()
            logger.warning("Extraction failed for entry %d: %s", entry_id, url)
            continue

        # RSS changed but extracted content is the same — just update source_hash
        if extracted["content_hash"] == stored_content_hash:
            await conn.execute(
                "UPDATE article_snapshots SET source_hash = %s "
                "WHERE entry_id = %s AND version = %s",
                (source_hash, entry_id, max_version),
            )
            await conn.commit()
            logger.info("RSS content changed but extracted content unchanged for entry %d", entry_id)
            continue

        try:
            await _store_snapshot(conn, entry_id, feed_id, url, extracted, source_hash, max_version + 1)
        except psycopg.errors.UniqueViolation:
            # The unique index is on (entry_id, content_hash), not scoped to
            # the latest version — if the publisher reverted an edit, the
            # re-fetched content can match an OLDER version's hash even though
            # it differs from the current latest one, colliding here. Not a
            # new version; just record that this RSS content now maps to
            # already-known article content, same as the "unchanged" branch
            # above.
            await conn.rollback()
            await conn.execute(
                "UPDATE article_snapshots SET source_hash = %s "
                "WHERE entry_id = %s AND version = %s",
                (source_hash, entry_id, max_version),
            )
            await conn.commit()
            continue
        processed += 1

    return processed


async def _process_retry_batch(conn: psycopg.AsyncConnection) -> int:
    """Retry entries that previously failed and are due (next_retry_at <= now,
    not given_up), for feeds that are still fetch_full_content-enabled. Driven
    entirely by extract_attempts — no Miniflux list query involved, so a retry
    can never be lost to a recency window again.

    The join on feed_config excludes a currently-disabled feed's rows from
    being selected at all, rather than selecting then skipping them in Python:
    a skipped row's next_retry_at is never advanced, so with the naive
    select-then-skip approach it would keep sorting to the front of every
    next_retry_at-ordered batch forever and starve every other due entry out
    of WORKER_EXTRACT_RETRY_BATCH. Excluding it in SQL leaves the row
    untouched in the DB (still not given_up) until the feed is re-enabled.
    """
    if WORKER_EXTRACT_RETRY_BATCH <= 0:
        return 0
    cur = await conn.execute(
        """
        SELECT a.entry_id, a.feed_id, fc.extract_rules
          FROM extract_attempts a
          JOIN feed_config fc ON fc.feed_id = a.feed_id AND fc.fetch_full_content = TRUE
         WHERE NOT a.given_up AND a.next_retry_at <= NOW()
         ORDER BY a.next_retry_at ASC LIMIT %s
        """,
        (WORKER_EXTRACT_RETRY_BATCH,),
    )
    due = await cur.fetchall()
    if not due:
        return 0

    processed = 0
    for r in due:
        entry_id, feed_id, rules = r["entry_id"], r["feed_id"], r["extract_rules"] or {}
        try:
            entry = await miniflux_client.get_entry(entry_id)
        except Exception as e:
            if _feed_gone_from_miniflux(e):
                await conn.execute("DELETE FROM extract_attempts WHERE entry_id = %s", (entry_id,))
                await conn.commit()
            else:
                logger.debug("Retry: failed to refetch entry %d: %s", entry_id, e)
            continue
        exists, *_ = await _get_snapshot_info(conn, entry_id)
        if exists:  # resolved via some other path since the failure
            await conn.execute("DELETE FROM extract_attempts WHERE entry_id = %s", (entry_id,))
            await conn.commit()
            continue
        url = entry.get("url", "")
        if url:
            await ensure_fresh_login(domain_from_url(url))
        if await _attempt_extraction(conn, entry, feed_id, rules):
            processed += 1
    return processed


async def process_new_entries() -> int:
    """Check for new entries in full-content feeds and extract them. Returns count processed."""
    processed = 0
    async with get_conn() as conn:
        enabled = await _get_enabled_feeds(conn)
        if not enabled:
            return 0

        for feed_id, config in enabled.items():
            if feed_id in _missing_feeds:
                continue  # deleted from Miniflux earlier this run — skip silently
            extract_rules = config["extract_rules"]
            try:
                processed += await _process_feed_cursor(conn, feed_id, extract_rules)
                processed += await _process_feed_recency(conn, feed_id, extract_rules)
            except Exception as e:
                # A failed statement leaves the shared connection's transaction
                # poisoned — every later query on it (other feeds, the retry
                # pass) would raise InFailedSqlTransaction otherwise.
                await conn.rollback()
                if _feed_gone_from_miniflux(e):
                    logger.warning(
                        "Feed %d no longer exists in Miniflux (deleted upstream); "
                        "skipping full-content fetch. Local config and snapshots are "
                        "retained.", feed_id)
                    _missing_feeds.add(feed_id)
                else:
                    logger.exception("Failed to process feed %d", feed_id)
                continue

        try:
            processed += await _process_retry_batch(conn)
        except Exception:
            await conn.rollback()
            logger.exception("Extraction retry pass failed")

    return processed


async def worker_loop() -> None:
    """Background loop that continuously processes new entries."""
    logger.info("Worker started, polling every %ds", WORKER_POLL_INTERVAL)
    while True:
        try:
            count = await process_new_entries()
            if count:
                logger.info("Processed %d new entries", count)
        except Exception:
            logger.exception("Worker error")
        try:
            # Embed pending articles + refresh the taste centroid (Part C phase 2).
            # No-op / fail-open when Ollama is disabled or unreachable.
            from app import embeddings
            await embeddings.embed_pending()
            # Sweep the archive a page at a time so related-articles works across
            # history. No-op once the walk is done.
            await embeddings.embed_backfill()
            await embeddings.recompute_centroid()
        except Exception:
            logger.exception("Embedding pass error")
        try:
            # Fold any newly-captured engagement signals into the ranker. No-op
            # when the ranker is disabled or the engine is down (events are kept).
            from app import ranker_client
            await ranker_client.sync_observations()
        except Exception:
            logger.exception("Ranker sync error")
        await asyncio.sleep(WORKER_POLL_INTERVAL)
