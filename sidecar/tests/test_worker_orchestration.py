"""Orchestration tests for the extraction retry/backoff fix, exercising
_process_feed_cursor / _process_feed_recency / _process_retry_batch end to end
against an in-memory fake connection (no real Postgres, no network).

The invariants covered here were exactly the ones a plain code read got wrong
in review before this file existed (see fix/extraction-retry history): the
discovery cursor must advance even over entries it skips, an entry already
being tracked in extract_attempts must never be re-attempted directly by the
discovery pass (including once given_up), and a temporarily disabled feed
must not lose its durable retry state.
"""
import asyncio
import contextlib
import hashlib
from datetime import UTC, datetime, timedelta

import psycopg

from app import worker


def run(coro):
    return asyncio.run(coro)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _entry(id, url="https://example.com/a", content="hi", created_at=None):
    return {
        "id": id,
        "url": url,
        "content": content,
        "created_at": created_at or _iso(datetime.now(UTC)),
    }


def _extracted(text="body"):
    return {
        "content_text": text,
        "content_html": f"<p>{text}</p>",
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "metadata": {},
    }


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _Poisoned(Exception):
    """Stand-in for psycopg's InFailedSqlTransaction: any statement issued on
    a connection with a failed, un-rolled-back transaction raises this."""


class _FakeConn:
    """In-memory model of exactly the tables worker.py touches."""

    def __init__(self):
        self.cursors: dict[int, int] = {}
        self.attempts: dict[int, dict] = {}
        self.snapshots: dict[int, list[dict]] = {}
        self.feed_config: dict[int, dict] = {}
        self.commits = 0
        self.rollbacks = 0
        self.poisoned = False
        # entry_ids that raise psycopg.errors.UniqueViolation exactly once the
        # next time INSERT INTO article_snapshots is attempted for them.
        self.unique_violation_once: set[int] = set()
        # feed_id -> Exception: raised once (then cleared), specifically from
        # _feed_cursor's lookup for that feed — lets a test fail exactly one
        # feed's processing (to test cross-feed rollback isolation) without
        # disturbing _get_enabled_feeds' own unrelated query.
        self.fail_for_feed_cursor: dict[int, Exception] = {}

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.poisoned = False

    async def execute(self, sql, params=()):
        if self.poisoned:
            raise _Poisoned("current transaction is aborted")
        s = " ".join(sql.split())
        params = tuple(params) if params else ()

        if s.startswith("SELECT cursor_entry_id FROM extract_cursor"):
            (feed_id,) = params
            if feed_id in self.fail_for_feed_cursor:
                self.poisoned = True
                raise self.fail_for_feed_cursor.pop(feed_id)
            if feed_id in self.cursors:
                return _FakeCursor([{"cursor_entry_id": self.cursors[feed_id]}])
            return _FakeCursor([])

        if s.startswith("INSERT INTO extract_cursor"):
            (feed_id,) = params
            self.cursors.setdefault(feed_id, 0)
            return _FakeCursor([])

        if s.startswith("UPDATE extract_cursor SET cursor_entry_id"):
            entry_id, feed_id = params
            self.cursors[feed_id] = max(self.cursors.get(feed_id, 0), entry_id)
            return _FakeCursor([])

        if s.startswith("SELECT source_hash, content_hash, version FROM article_snapshots"):
            (entry_id,) = params
            versions = self.snapshots.get(entry_id)
            if not versions:
                return _FakeCursor([])
            latest = versions[-1]
            return _FakeCursor([{
                "source_hash": latest["source_hash"],
                "content_hash": latest["content_hash"],
                "version": latest["version"],
            }])

        if s.startswith("INSERT INTO article_snapshots"):
            entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version, source_hash = params
            if entry_id in self.unique_violation_once:
                self.unique_violation_once.discard(entry_id)
                self.poisoned = True
                raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")
            self.snapshots.setdefault(entry_id, []).append({
                "version": version, "content_hash": content_hash, "source_hash": source_hash,
            })
            return _FakeCursor([])

        if s.startswith("UPDATE article_snapshots SET source_hash"):
            source_hash, entry_id, version = params
            for v in self.snapshots.get(entry_id, []):
                if v["version"] == version:
                    v["source_hash"] = source_hash
            return _FakeCursor([])

        if s.startswith("SELECT 1 FROM extract_attempts"):
            (entry_id,) = params
            return _FakeCursor([{"exists": 1}] if entry_id in self.attempts else [])

        if s.startswith("INSERT INTO extract_attempts"):
            entry_id, feed_id, error = params
            prior = self.attempts.get(entry_id)
            count = (prior["attempt_count"] + 1) if prior else 1
            self.attempts[entry_id] = {
                "feed_id": feed_id,
                "attempt_count": count,
                "last_error": error,
                "given_up": prior["given_up"] if prior else False,
                "next_retry_at": prior["next_retry_at"] if prior else None,
            }
            return _FakeCursor([{"attempt_count": count}])

        if s.startswith("UPDATE extract_attempts SET next_retry_at"):
            next_retry_at, given_up, entry_id = params
            self.attempts[entry_id]["next_retry_at"] = next_retry_at
            self.attempts[entry_id]["given_up"] = given_up
            return _FakeCursor([])

        if s.startswith("DELETE FROM extract_attempts"):
            (entry_id,) = params
            self.attempts.pop(entry_id, None)
            return _FakeCursor([])

        if s.startswith("SELECT a.entry_id, a.feed_id, fc.extract_rules"):
            # Retry-batch due query: JOINs feed_config, so a disabled feed's
            # rows are excluded from selection entirely (not selected-then-
            # skipped) — see _process_retry_batch's docstring for why that
            # distinction matters.
            (limit,) = params
            now = datetime.now(UTC)
            due = [
                (a["next_retry_at"], {
                    "entry_id": eid, "feed_id": a["feed_id"],
                    "extract_rules": self.feed_config.get(a["feed_id"]),
                })
                for eid, a in self.attempts.items()
                if not a["given_up"] and a["next_retry_at"] is not None and a["next_retry_at"] <= now
                and a["feed_id"] in self.feed_config
            ]
            due.sort(key=lambda t: t[0])
            return _FakeCursor([row for _, row in due[:limit]])

        if s == "SELECT feed_id, extract_rules FROM feed_config WHERE fetch_full_content = TRUE":
            return _FakeCursor([
                {"feed_id": fid, "extract_rules": rules} for fid, rules in self.feed_config.items()
            ])

        raise AssertionError(f"unexpected SQL in fake conn: {s!r}")


def _patch_common(monkeypatch, conn, get_entries=None, get_entry=None, fetch_and_extract=None):
    async def noop_login(domain):
        return None

    async def no_cookies(url):
        return None

    monkeypatch.setattr(worker, "ensure_fresh_login", noop_login)
    monkeypatch.setattr(worker, "get_cookies_for_url", no_cookies)
    if get_entries is not None:
        monkeypatch.setattr(worker.miniflux_client, "get_entries", get_entries)
    if get_entry is not None:
        monkeypatch.setattr(worker.miniflux_client, "get_entry", get_entry)
    if fetch_and_extract is not None:
        monkeypatch.setattr(worker, "fetch_and_extract", fetch_and_extract)


# ---------------------------------------------------------------------------
# _process_feed_cursor
# ---------------------------------------------------------------------------

def test_cursor_extracts_fresh_entry_and_advances(monkeypatch):
    conn = _FakeConn()
    entries = [_entry(10)]

    async def get_entries(**kw):
        assert kw["after_entry_id"] == 0
        return {"entries": entries}

    async def extract(url, rules, cookies=None):
        return _extracted()

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_cursor(conn, feed_id=1, extract_rules={}))
    assert processed == 1
    assert conn.cursors[1] == 10
    assert 10 in conn.snapshots
    assert 10 not in conn.attempts  # cleared on success


def test_cursor_advances_past_already_snapshotted_entry(monkeypatch):
    # The critical invariant: an entry the cursor pass skips (because it
    # already has a snapshot) must still advance the cursor past it, or the
    # discovery walk gets permanently stuck behind it.
    conn = _FakeConn()
    conn.snapshots[5] = [{"version": 1, "content_hash": "h", "source_hash": "s"}]
    entries = [_entry(5), _entry(6)]

    async def get_entries(**kw):
        return {"entries": entries}

    calls = []

    async def extract(url, rules, cookies=None):
        calls.append(url)
        return _extracted()

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_cursor(conn, feed_id=1, extract_rules={}))
    assert conn.cursors[1] == 6  # advanced past entry 5 despite skipping it
    assert processed == 1  # only entry 6 was actually extracted
    assert calls == ["https://example.com/a"]  # entry 5 never triggered a fetch


def test_cursor_skips_and_logs_too_old_entry_without_attempting(monkeypatch, caplog):
    conn = _FakeConn()
    old_entry = _entry(7, created_at=_iso(datetime.now(UTC) - timedelta(days=10)))

    async def get_entries(**kw):
        return {"entries": [old_entry]}

    async def extract(url, rules, cookies=None):
        raise AssertionError("must not attempt extraction for an archive-backfill-skipped entry")

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    with caplog.at_level("INFO"):
        processed = run(worker._process_feed_cursor(conn, feed_id=1, extract_rules={}))
    assert processed == 0
    assert conn.cursors[1] == 7
    assert 7 not in conn.attempts
    assert any("Archive backfill skip" in r.message for r in caplog.records)


def test_cursor_defers_to_retry_pass_for_entry_with_attempt_record(monkeypatch):
    # Including once given_up=True: the discovery pass must never re-decide an
    # entry's fate once anything else (e.g. the backfill script) has touched it.
    conn = _FakeConn()
    conn.attempts[8] = {
        "feed_id": 1, "attempt_count": 9, "last_error": "x",
        "given_up": True, "next_retry_at": None,
    }
    entry = _entry(8)

    async def get_entries(**kw):
        return {"entries": [entry]}

    async def extract(url, rules, cookies=None):
        raise AssertionError("discovery pass must not re-attempt an entry already owned by the retry pass")

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_cursor(conn, feed_id=1, extract_rules={}))
    assert processed == 0
    assert conn.cursors[1] == 8
    assert conn.attempts[8]["given_up"] is True  # untouched


# ---------------------------------------------------------------------------
# _attempt_extraction (shared by the cursor and retry passes)
# ---------------------------------------------------------------------------

def test_attempt_extraction_handles_concurrent_unique_violation(monkeypatch):
    # The live worker and the one-off backfill script (scripts/backfill_
    # orphaned_extractions.py) can extract the same never-snapshotted entry at
    # the same time. Whichever call loses the race must not crash — it should
    # recognize the snapshot now exists (via someone else) and still report
    # success.
    conn = _FakeConn()
    conn.unique_violation_once.add(50)
    entry = _entry(50)

    async def extract(url, rules, cookies=None):
        return _extracted()

    _patch_common(monkeypatch, conn, fetch_and_extract=extract)

    ok = run(worker._attempt_extraction(conn, entry, feed_id=1, extract_rules={}))
    assert ok is True
    assert 50 not in conn.attempts  # no failure recorded — this is a success, not an error
    assert conn.rollbacks == 1
    assert conn.poisoned is False


# ---------------------------------------------------------------------------
# _process_feed_recency
# ---------------------------------------------------------------------------

def test_recency_refetches_on_rss_content_change(monkeypatch):
    conn = _FakeConn()
    conn.snapshots[20] = [{"version": 1, "content_hash": "old-hash", "source_hash": "old-source"}]
    entry = _entry(20, content="new rss body")

    async def get_entries(**kw):
        return {"entries": [entry]}

    async def extract(url, rules, cookies=None):
        return _extracted("new full text")

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_recency(conn, feed_id=1, extract_rules={}))
    assert processed == 1
    assert len(conn.snapshots[20]) == 2
    assert conn.snapshots[20][-1]["version"] == 2


def test_recency_skips_never_snapshotted_entry(monkeypatch):
    conn = _FakeConn()
    entry = _entry(21)

    async def get_entries(**kw):
        return {"entries": [entry]}

    async def extract(url, rules, cookies=None):
        raise AssertionError("recency pass must leave never-snapshotted entries to the cursor pass")

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_recency(conn, feed_id=1, extract_rules={}))
    assert processed == 0
    assert 21 not in conn.snapshots


def test_recency_handles_content_reverted_to_older_version(monkeypatch):
    # entry_id 22 has v1 (hash "hash-a") and latest v2 (hash "hash-b"). The
    # publisher reverts their edit, so re-extraction yields "hash-a" again —
    # that differs from the *latest* stored hash (so the "unchanged" shortcut
    # doesn't fire) but collides with v1's hash on the (entry_id, content_hash)
    # unique index when a naive INSERT for v3 is attempted.
    conn = _FakeConn()
    conn.snapshots[22] = [
        {"version": 1, "content_hash": "hash-a", "source_hash": "source-1"},
        {"version": 2, "content_hash": "hash-b", "source_hash": "source-2"},
    ]
    conn.unique_violation_once.add(22)
    entry = _entry(22, content="reverted rss body")

    async def get_entries(**kw):
        return {"entries": [entry]}

    async def extract(url, rules, cookies=None):
        return {
            "content_text": "a", "content_html": "<p>a</p>",
            "content_hash": "hash-a", "metadata": {},
        }

    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker._process_feed_recency(conn, feed_id=1, extract_rules={}))
    assert processed == 0  # not counted as a new version
    assert len(conn.snapshots[22]) == 2  # no duplicate/v3 row was created
    assert conn.rollbacks == 1  # the failed INSERT was rolled back
    assert conn.poisoned is False  # ...and the connection is usable again
    new_source_hash = hashlib.sha256(entry["content"].encode()).hexdigest()
    assert conn.snapshots[22][-1]["source_hash"] == new_source_hash  # v2's source_hash updated instead


# ---------------------------------------------------------------------------
# _process_retry_batch
# ---------------------------------------------------------------------------

def _seed_due_attempt(conn, entry_id, feed_id, given_up=False, next_retry_at=None):
    conn.attempts[entry_id] = {
        "feed_id": feed_id, "attempt_count": 1, "last_error": "x",
        "given_up": given_up,
        "next_retry_at": next_retry_at or (datetime.now(UTC) - timedelta(minutes=1)),
    }


def test_retry_batch_succeeds_and_clears_attempt(monkeypatch):
    conn = _FakeConn()
    conn.feed_config[1] = {}
    _seed_due_attempt(conn, 30, feed_id=1)
    login_calls = []

    async def get_entry(entry_id):
        return _entry(entry_id)

    async def extract(url, rules, cookies=None):
        return _extracted()

    async def login(domain):
        login_calls.append(domain)

    _patch_common(monkeypatch, conn, get_entry=get_entry, fetch_and_extract=extract)
    monkeypatch.setattr(worker, "ensure_fresh_login", login)

    processed = run(worker._process_retry_batch(conn))
    assert processed == 1
    assert 30 not in conn.attempts
    assert 30 in conn.snapshots
    assert login_calls  # ensure_fresh_login was called before the retry fetch


def test_retry_batch_excludes_given_up_and_not_yet_due(monkeypatch):
    conn = _FakeConn()
    conn.feed_config[1] = {}
    _seed_due_attempt(conn, 31, feed_id=1, given_up=True)
    _seed_due_attempt(conn, 32, feed_id=1, next_retry_at=datetime.now(UTC) + timedelta(hours=1))

    async def get_entry(entry_id):
        raise AssertionError("must not fetch an excluded entry")

    _patch_common(monkeypatch, conn, get_entry=get_entry)

    processed = run(worker._process_retry_batch(conn))
    assert processed == 0
    assert 31 in conn.attempts and 32 in conn.attempts  # both untouched


def test_retry_batch_leaves_row_when_feed_disabled(monkeypatch):
    conn = _FakeConn()
    # No entry in conn.feed_config → feed currently disabled/not full-content.
    _seed_due_attempt(conn, 33, feed_id=1)

    async def get_entry(entry_id):
        raise AssertionError("must not fetch for a disabled feed")

    _patch_common(monkeypatch, conn, get_entry=get_entry)

    processed = run(worker._process_retry_batch(conn))
    assert processed == 0
    assert 33 in conn.attempts  # NOT deleted — durable state survives a temporary disable


def test_retry_batch_drops_row_when_feed_gone_from_miniflux(monkeypatch):
    import httpx

    conn = _FakeConn()
    conn.feed_config[1] = {}
    _seed_due_attempt(conn, 34, feed_id=1)

    async def get_entry(entry_id):
        req = httpx.Request("GET", "http://x/v1/entries/34")
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    _patch_common(monkeypatch, conn, get_entry=get_entry)

    processed = run(worker._process_retry_batch(conn))
    assert processed == 0
    assert 34 not in conn.attempts


def test_retry_batch_drops_row_when_already_resolved(monkeypatch):
    conn = _FakeConn()
    conn.feed_config[1] = {}
    conn.snapshots[35] = [{"version": 1, "content_hash": "h", "source_hash": "s"}]
    _seed_due_attempt(conn, 35, feed_id=1)

    async def get_entry(entry_id):
        return _entry(entry_id)

    async def extract(url, rules, cookies=None):
        raise AssertionError("must not re-extract an already-resolved entry")

    _patch_common(monkeypatch, conn, get_entry=get_entry, fetch_and_extract=extract)

    processed = run(worker._process_retry_batch(conn))
    assert processed == 0
    assert 35 not in conn.attempts


# ---------------------------------------------------------------------------
# give-up logging (via _record_attempt_failure, the shared failure path)
# ---------------------------------------------------------------------------

def test_give_up_warning_fires_exactly_once(monkeypatch, caplog):
    from app.config import WORKER_EXTRACT_MAX_ATTEMPTS

    conn = _FakeConn()
    with caplog.at_level("WARNING"):
        for _ in range(WORKER_EXTRACT_MAX_ATTEMPTS):
            run(worker._record_attempt_failure(conn, entry_id=40, feed_id=1, error="boom"))

    give_up_lines = [r for r in caplog.records if "Giving up on entry 40" in r.message]
    assert len(give_up_lines) == 1
    assert conn.attempts[40]["given_up"] is True
    assert conn.attempts[40]["attempt_count"] == WORKER_EXTRACT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# process_new_entries: cross-feed rollback isolation
# ---------------------------------------------------------------------------

def test_process_new_entries_isolates_feed_failure_via_rollback(monkeypatch):
    # Feed 1 fails with a plain DB error partway through (simulating e.g. a
    # constraint violation the inner handlers don't already catch) and poisons
    # the shared connection. Without process_new_entries' rollback, every
    # later statement on that connection — including feed 2's — would raise
    # InFailedSqlTransaction. With it, feed 2 must still process normally.
    conn = _FakeConn()
    conn.feed_config = {1: {}, 2: {}}
    conn.fail_for_feed_cursor[1] = RuntimeError("simulated DB error for feed 1")

    async def get_entries(**kw):
        feed_id = kw["feed_id"]
        return {"entries": [_entry(100 + feed_id, url=f"https://example.com/{feed_id}")]}

    async def extract(url, rules, cookies=None):
        return _extracted()

    @contextlib.asynccontextmanager
    async def fake_get_conn():
        yield conn

    monkeypatch.setattr(worker, "get_conn", fake_get_conn)
    _patch_common(monkeypatch, conn, get_entries=get_entries, fetch_and_extract=extract)

    processed = run(worker.process_new_entries())

    assert conn.rollbacks >= 1
    assert conn.poisoned is False
    assert 1 not in conn.cursors  # feed 1's failure happened before its cursor could be read/saved
    assert conn.cursors.get(2) == 102  # feed 2 processed normally despite feed 1's failure
    assert processed == 1  # only feed 2's entry was extracted
