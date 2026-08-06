"""Unit tests for the extraction retry/backoff fix (worker.process_new_entries).

Covers the two pure decision functions only — everything else in the fix is
DB/Miniflux-touching orchestration, exercised live (no DB fixtures exist in
this suite; see test_worker_missing_feed.py for the established convention).
"""
from datetime import UTC, datetime, timedelta

from app import worker


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_too_old_to_backfill_missing_created_at():
    assert worker._too_old_to_backfill({}) is False


def test_too_old_to_backfill_malformed_created_at():
    assert worker._too_old_to_backfill({"created_at": "not-a-date"}) is False


def test_too_old_to_backfill_recent_entry():
    entry = {"created_at": _iso(datetime.now(UTC) - timedelta(hours=1))}
    assert worker._too_old_to_backfill(entry) is False


def test_too_old_to_backfill_old_entry():
    entry = {"created_at": _iso(datetime.now(UTC) - timedelta(days=10))}
    assert worker._too_old_to_backfill(entry) is True


def test_too_old_to_backfill_disabled(monkeypatch):
    monkeypatch.setattr(worker, "_BACKFILL_MAX_AGE", None)
    entry = {"created_at": _iso(datetime.now(UTC) - timedelta(days=365))}
    assert worker._too_old_to_backfill(entry) is False


def test_too_old_to_backfill_reads_created_at_not_published_at():
    # Regression guard: an entry with an old published_at but a recent
    # created_at (e.g. a Miniflux sync gap) must NOT be judged too old — that
    # was the original bug (entry 53083 and siblings synced ~4 days late).
    entry = {
        "published_at": _iso(datetime.now(UTC) - timedelta(days=30)),
        "created_at": _iso(datetime.now(UTC) - timedelta(hours=1)),
    }
    assert worker._too_old_to_backfill(entry) is False


def test_next_attempt_first_failure():
    given_up, delay = worker._next_attempt(1, max_attempts=5, base_min=5, cap_min=480)
    assert (given_up, delay) == (False, timedelta(minutes=5))


def test_next_attempt_backoff_doubles():
    given_up, delay = worker._next_attempt(2, max_attempts=5, base_min=5, cap_min=480)
    assert (given_up, delay) == (False, timedelta(minutes=10))
    given_up, delay = worker._next_attempt(3, max_attempts=5, base_min=5, cap_min=480)
    assert (given_up, delay) == (False, timedelta(minutes=20))


def test_next_attempt_caps_backoff():
    given_up, delay = worker._next_attempt(21, max_attempts=99, base_min=5, cap_min=480)
    assert given_up is False
    assert delay == timedelta(minutes=480)


def test_next_attempt_cap_is_reachable_at_default_max_attempts():
    # Regression guard: MAX_ATTEMPTS must be high enough that the backoff cap
    # actually binds at least once before giving up — otherwise BACKOFF_MAX_MIN
    # is dead config (flagged in review: defaults of max_attempts=5/base=5/cap=480
    # gave up after ~75min, well short of ever reaching the 480min cap).
    from app.config import (
        WORKER_EXTRACT_BACKOFF_BASE_MIN,
        WORKER_EXTRACT_BACKOFF_MAX_MIN,
        WORKER_EXTRACT_MAX_ATTEMPTS,
    )
    delays = []
    for count in range(1, WORKER_EXTRACT_MAX_ATTEMPTS):
        given_up, delay = worker._next_attempt(count)
        assert given_up is False
        delays.append(delay)
    given_up, delay = worker._next_attempt(WORKER_EXTRACT_MAX_ATTEMPTS)
    assert given_up is True and delay is None
    assert max(delays) == timedelta(minutes=WORKER_EXTRACT_BACKOFF_MAX_MIN)
    assert delays[0] == timedelta(minutes=WORKER_EXTRACT_BACKOFF_BASE_MIN)


def test_next_attempt_gives_up_at_max():
    given_up, delay = worker._next_attempt(5, max_attempts=5, base_min=5, cap_min=480)
    assert (given_up, delay) == (True, None)


def test_next_attempt_max_attempts_one_gives_up_immediately():
    given_up, delay = worker._next_attempt(1, max_attempts=1, base_min=5, cap_min=480)
    assert (given_up, delay) == (True, None)
