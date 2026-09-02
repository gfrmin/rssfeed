"""The publishing-cadence lookup, and its refusal to take the reader down."""
import asyncio

import pytest

from app import cadence


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    cadence.invalidate()
    yield
    cadence.invalidate()


async def _rows():
    return {1: {"latest": None, "median_gap_s": 3600.0}}


async def _boom():
    raise RuntimeError("entries is locked")


def test_cadence_returns_what_the_query_found(monkeypatch):
    monkeypatch.setattr(cadence, "_query", _rows)
    assert run(cadence.all_feeds()) == {1: {"latest": None, "median_gap_s": 3600.0}}


def test_cadence_is_cached_rather_than_rescanned_per_request(monkeypatch):
    calls = []

    async def counted():
        calls.append(1)
        return {}

    monkeypatch.setattr(cadence, "_query", counted)
    run(cadence.all_feeds())
    run(cadence.all_feeds())
    run(cadence.all_feeds())
    assert len(calls) == 1


def test_a_failed_cadence_query_does_not_take_the_reader_down(monkeypatch):
    """The sidebar reads this on every navigation. A locked table, a slow scan,
    a migration in flight — none of that is a reason the reader cannot open.
    Feeds simply lose the `quiet` state until it recovers."""
    monkeypatch.setattr(cadence, "_query", _boom)
    assert run(cadence.all_feeds()) == {}


def test_a_failure_is_not_cached_for_as_long_as_a_success(monkeypatch):
    monkeypatch.setattr(cadence, "_query", _boom)
    run(cadence.all_feeds())
    monkeypatch.setattr(cadence, "_query", _rows)
    assert run(cadence.all_feeds()) != {}
