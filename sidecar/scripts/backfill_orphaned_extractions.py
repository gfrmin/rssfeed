#!/usr/bin/env python
"""One-off backlog remediation for entries orphaned by the extraction-retry fix.

Entries in fetch_full_content feeds that have no article_snapshots row: under
the corrected worker.py semantics, an entry only gets auto-extracted if it was
already fresh (by Miniflux created_at) the first time the worker's per-feed
cursor reached it — entries that were already old before the fix shipped stay
correctly, permanently un-attempted by the live worker's normal walk. This
script is the explicit, one-time way to give the existing backlog its first
attempt.

Queries Postgres directly (the entry content is already in the shared
`entries` table — no Miniflux REST calls needed), then attempts extraction for
each candidate via the same code path (app.worker._attempt_extraction) and
per-domain throttle as the live worker, so a permanently-broken URL converges
to extract_attempts.given_up = TRUE and stops being retried by either this
script or the live worker rather than looping forever.

Resumable: Ctrl-C and rerun just re-queries "no snapshot yet, not given up".

NOTE: extractor.py's per-domain throttle is in-process, so running this
concurrently with the live sidecar can briefly double the effective rate to
any one domain. Fine at this app's scale, but `systemctl --user stop
rssfeed-sidecar` first if you'd rather avoid it.

    uv run python scripts/backfill_orphaned_extractions.py --dry-run
    uv run python scripts/backfill_orphaned_extractions.py
    uv run python scripts/backfill_orphaned_extractions.py --feed-id 156 --limit 50
    uv run python scripts/backfill_orphaned_extractions.py --include-given-up
"""
import argparse
import asyncio

from app import worker
from app.db import get_conn, run_migrations


async def _orphaned_entries(conn, feed_id: int | None, limit: int, include_given_up: bool):
    """Entries with fetch_full_content enabled and no article_snapshots row yet,
    oldest entry id first.

    Also respects any in-flight extract_attempts state, same as the live
    worker's own retry pass would: an entry mid-backoff (not given_up, but not
    due yet) is skipped so this script can't burn through its bounded retry
    budget early by racing ahead of the schedule. include_given_up=True
    overrides only the given_up exclusion, not the backoff one.
    """
    where = ["s.entry_id IS NULL"]
    params: list = []
    if feed_id is not None:
        where.append("e.feed_id = %s")
        params.append(feed_id)
    if include_given_up:
        where.append(
            "NOT EXISTS (SELECT 1 FROM extract_attempts a WHERE a.entry_id = e.id "
            "AND NOT a.given_up AND a.next_retry_at > NOW())"
        )
    else:
        where.append(
            "NOT EXISTS (SELECT 1 FROM extract_attempts a WHERE a.entry_id = e.id "
            "AND (a.given_up OR a.next_retry_at > NOW()))"
        )
    params.append(limit)
    query = f"""
        SELECT e.id, e.feed_id, e.url, e.content, e.created_at, fc.extract_rules
          FROM entries e
          JOIN feed_config fc ON fc.feed_id = e.feed_id AND fc.fetch_full_content = TRUE
          LEFT JOIN article_snapshots s ON s.entry_id = e.id
         WHERE {" AND ".join(where)}
         ORDER BY e.id
         LIMIT %s
    """
    cur = await conn.execute(query, params)
    return await cur.fetchall()


async def _run(feed_id: int | None, limit: int, dry_run: bool, include_given_up: bool) -> None:
    run_migrations()
    async with get_conn() as conn:
        rows = await _orphaned_entries(conn, feed_id, limit, include_given_up)
        if not rows:
            print("No orphaned entries found.")
            return

        scope = f" for feed {feed_id}" if feed_id is not None else ""
        print(f"{len(rows)} orphaned entr{'y' if len(rows) == 1 else 'ies'} found{scope}.")
        if dry_run:
            for r in rows:
                print(f"  entry {r['id']} (feed {r['feed_id']}): {r['url']}")
            print("Dry run — nothing fetched.")
            return

        succeeded = failed = 0
        for r in rows:
            entry = {
                "id": r["id"],
                "url": r["url"],
                "content": r["content"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            try:
                await worker.ensure_fresh_login(worker.domain_from_url(r["url"]))
                ok = await worker._attempt_extraction(conn, entry, r["feed_id"], r["extract_rules"] or {})
            except Exception:
                # Don't let one bad row (unexpected DB error, etc.) abort the
                # whole batch — un-poison the connection and keep going; the
                # entry is simply retried on the next run.
                await conn.rollback()
                ok = False
            succeeded += ok
            failed += not ok
            print(f"  entry {r['id']}: {'OK' if ok else 'failed'}")

        print(
            f"\n{succeeded} extracted, {failed} failed this pass "
            "(failures accumulate toward extract_attempts.given_up — rerun later to retry)."
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed-id", type=int, default=None, help="Only process this Miniflux feed id")
    p.add_argument("--limit", type=int, default=200, help="Max entries to process this run (default 200)")
    p.add_argument("--dry-run", action="store_true", help="List candidates without fetching")
    p.add_argument(
        "--include-given-up", action="store_true",
        help="Also retry entries already marked given_up (e.g. after fixing a site-specific bug)",
    )
    args = p.parse_args()
    asyncio.run(_run(args.feed_id, args.limit, args.dry_run, args.include_given_up))


if __name__ == "__main__":
    main()
