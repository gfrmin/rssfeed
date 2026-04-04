import asyncio
import hashlib
import logging

import psycopg

from app import miniflux_client, llm
from app.config import WORKER_POLL_INTERVAL
from app.db import get_conn
from app.extractor import fetch_and_extract

logger = logging.getLogger(__name__)


async def _get_enabled_feeds(conn: psycopg.AsyncConnection) -> dict[int, dict]:
    """Return {feed_id: {extract_rules, summarize}} for feeds with full content enabled."""
    cur = await conn.execute(
        "SELECT feed_id, extract_rules, summarize FROM feed_config WHERE fetch_full_content = TRUE"
    )
    return {
        row["feed_id"]: {"extract_rules": row["extract_rules"] or {}, "summarize": row["summarize"]}
        for row in await cur.fetchall()
    }


async def _get_snapshot_info(conn: psycopg.AsyncConnection, entry_id: int) -> tuple[bool, str | None, int]:
    """Return (exists, source_hash, max_version) for the latest snapshot of an entry."""
    cur = await conn.execute(
        "SELECT source_hash, version FROM article_snapshots WHERE entry_id = %s ORDER BY version DESC LIMIT 1",
        (entry_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False, None, 0
    return True, row["source_hash"], row["version"]


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


async def _run_llm_tasks(conn: psycopg.AsyncConnection, entry_id: int, text: str, do_summarize: bool) -> dict:
    """Run LLM summarization, tagging, and embedding for an entry."""
    metadata_updates = {}

    # Summarize
    if do_summarize and text:
        summary = await llm.summarize(text)
        if summary:
            metadata_updates["summary"] = summary
            await conn.execute(
                "UPDATE article_snapshots SET metadata = metadata || %s::jsonb "
                "WHERE entry_id = %s AND version = (SELECT MAX(version) FROM article_snapshots WHERE entry_id = %s)",
                (psycopg.types.json.Json({"summary": summary}), entry_id, entry_id),
            )

    # Auto-tag
    if text:
        tags = await llm.classify(text)
        for tag in tags:
            await conn.execute(
                "INSERT INTO article_tags (entry_id, tag) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (entry_id, tag),
            )

    # Generate embedding
    if text:
        embedding = await llm.embed(text)
        if embedding:
            await conn.execute(
                "INSERT INTO article_embeddings (entry_id, embedding) VALUES (%s, %s) "
                "ON CONFLICT (entry_id) DO UPDATE SET embedding = %s",
                (entry_id, embedding, embedding),
            )

    await conn.commit()
    return metadata_updates


async def _apply_filters(conn: psycopg.AsyncConnection, entry: dict) -> None:
    """Apply saved filter rules to an entry."""
    from app.routes.filters import matches_rules

    cur = await conn.execute("SELECT * FROM saved_filters")
    filters = await cur.fetchall()

    for f in filters:
        rules = f.get("rules", [])
        action = f.get("auto_action", "")
        if not rules or not action:
            continue

        if matches_rules(entry, rules):
            entry_id = entry["id"]
            if action == "mark_read":
                await miniflux_client.update_entry_status([entry_id], "read")
                logger.info("Filter '%s' marked entry %d as read", f["name"], entry_id)
            elif action == "star":
                await miniflux_client.toggle_bookmark(entry_id)
                logger.info("Filter '%s' starred entry %d", f["name"], entry_id)


async def process_new_entries() -> int:
    """Check for new entries in full-content feeds and extract them. Returns count processed."""
    processed = 0
    async with get_conn() as conn:
        enabled = await _get_enabled_feeds(conn)
        if not enabled:
            return 0

        for feed_id, config in enabled.items():
            extract_rules = config["extract_rules"]
            do_summarize = config.get("summarize", False)

            try:
                data = await miniflux_client.get_entries(feed_id=feed_id, limit=50)
            except Exception:
                logger.exception("Failed to fetch entries for feed %d", feed_id)
                continue

            for entry in data.get("entries", []):
                entry_id = entry["id"]
                url = entry.get("url", "")
                if not url:
                    continue

                source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()
                exists, stored_hash, max_version = await _get_snapshot_info(conn, entry_id)

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

                extracted = await fetch_and_extract(url, extract_rules)
                if extracted:
                    await _store_snapshot(conn, entry_id, feed_id, url, extracted, source_hash, next_version)
                    processed += 1

                    # Run LLM tasks (non-blocking — failures are logged, not raised)
                    try:
                        await _run_llm_tasks(conn, entry_id, extracted["content_text"], do_summarize)
                    except Exception:
                        logger.exception("LLM tasks failed for entry %d", entry_id)

                    # Apply filter rules (only for new entries, not re-fetches)
                    if not exists:
                        try:
                            await _apply_filters(conn, entry)
                        except Exception:
                            logger.exception("Filter application failed for entry %d", entry_id)
                else:
                    logger.warning("Extraction failed for entry %d: %s", entry_id, url)

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
        await asyncio.sleep(WORKER_POLL_INTERVAL)
