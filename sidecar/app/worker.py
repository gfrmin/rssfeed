import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

import psycopg

from app import browser_login, credvault, miniflux_client
from app.config import WORKER_POLL_INTERVAL
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
        datetime.now(timezone.utc) - meta["updated_at"] < _RELOGIN_STALE_AFTER
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


async def process_new_entries() -> int:
    """Check for new entries in full-content feeds and extract them. Returns count processed."""
    processed = 0
    async with get_conn() as conn:
        enabled = await _get_enabled_feeds(conn)
        if not enabled:
            return 0

        for feed_id, config in enabled.items():
            extract_rules = config["extract_rules"]

            try:
                data = await miniflux_client.get_entries(feed_id=feed_id, limit=50)
            except Exception:
                logger.exception("Failed to fetch entries for feed %d", feed_id)
                continue

            entries = data.get("entries", [])
            # Refresh a stored paywall login before fetching this feed's articles.
            first_url = next((e.get("url") for e in entries if e.get("url")), None)
            if first_url:
                await ensure_fresh_login(domain_from_url(first_url))

            for entry in entries:
                entry_id = entry["id"]
                url = entry.get("url", "")
                if not url:
                    continue

                source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()
                exists, stored_hash, stored_content_hash, max_version = await _get_snapshot_info(conn, entry_id)

                if exists and stored_hash == source_hash:
                    continue  # No change in RSS content

                if exists and stored_hash is None:
                    # Backfill source_hash for pre-existing snapshots (no re-fetch)
                    await conn.execute(
                        "UPDATE article_snapshots SET source_hash = %s "
                        "WHERE entry_id = %s AND version = %s",
                        (source_hash, entry_id, max_version),
                    )
                    await conn.commit()
                    continue

                next_version = max_version + 1 if exists else 1
                if exists:
                    logger.info("RSS content changed for entry %d, re-fetching: %s", entry_id, url)
                else:
                    logger.info("Extracting entry %d: %s", entry_id, url)

                try:
                    cookies = await get_cookies_for_url(url)
                    extracted = await fetch_and_extract(url, extract_rules, cookies=cookies)
                except Exception:
                    logger.exception("Extraction crashed for entry %d: %s", entry_id, url)
                    extracted = None
                if not extracted:
                    # Update source_hash to prevent infinite retry on extraction failure
                    if exists:
                        await conn.execute(
                            "UPDATE article_snapshots SET source_hash = %s "
                            "WHERE entry_id = %s AND version = %s",
                            (source_hash, entry_id, max_version),
                        )
                        await conn.commit()
                    logger.warning("Extraction failed for entry %d: %s", entry_id, url)
                    continue

                # RSS changed but extracted content is the same — just update source_hash
                if exists and extracted["content_hash"] == stored_content_hash:
                    await conn.execute(
                        "UPDATE article_snapshots SET source_hash = %s "
                        "WHERE entry_id = %s AND version = %s",
                        (source_hash, entry_id, max_version),
                    )
                    await conn.commit()
                    logger.info("RSS content changed but extracted content unchanged for entry %d", entry_id)
                    continue

                await _store_snapshot(conn, entry_id, feed_id, url, extracted, source_hash, next_version)
                processed += 1

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
