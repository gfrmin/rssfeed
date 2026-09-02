"""Test bootstrap.

app.config reads its environment at import time, so anything the app reads on
import has to be pinned here, before any app import. These unit tests only
exercise pure logic — no connection is opened.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# Pin login recipes to a path that cannot exist. Without this the suite loads
# whatever the developer happens to have in ~/.config/rssfeed/login-recipes.json —
# so the tests would read real operator config, pass on that machine, and behave
# differently in CI. Tests that care about recipes inject their own via monkeypatch.
os.environ.setdefault("LOGIN_RECIPES_FILE", "/nonexistent/test-login-recipes.json")


# ---------------------------------------------------------------------------
# A hermetic app under test: no Postgres, no Miniflux, no network. Shared by
# every test that drives a page rather than a function -- the feed set below is
# deliberately unwell in several different ways at once, because most of what
# these pages do is decide how to present that.
#
# test_routes_smoke.py defines its own, simpler `client`; a module-level fixture
# shadows this one, which is what that file wants.
# ---------------------------------------------------------------------------
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import cadence, credvault, embeddings, miniflux_client, ranker_client
from app.main import app
from app.routes import cookies as cookies_routes
from app.routes import entries as entries_routes
from app.routes import feeds as feeds_routes

NOW = datetime.now(UTC)

CLOUDFLARE = ("This website is protected by a Cloudflare bot challenge (CAPTCHA "
              "or JavaScript verification). Miniflux cannot solve this challenge "
              "automatically.")
FORBIDDEN = ("Access to this website is forbidden. Perhaps, this website has a "
             "bot protection mechanism?")
AUTH = ("Access to this website is not authorized. It could be a bad username "
        "or password.")


def _feed(fid, title, *, msg="", count=0, disabled=False):
    return {
        "id": fid, "title": title, "site_url": f"https://{fid}.example/",
        "feed_url": f"https://{fid}.example/rss",
        "checked_at": (NOW - timedelta(minutes=10)).isoformat(),
        "parsing_error_count": count, "parsing_error_message": msg,
        "disabled": disabled, "category": {"id": 1, "title": "News"},
    }


FEEDS = [
    _feed(1, "Challenged One", msg=CLOUDFLARE, count=9),
    _feed(2, "Challenged Two", msg=CLOUDFLARE, count=9),
    _feed(3, "Refused One", msg=FORBIDDEN, count=40),
    _feed(4, "Refused Two", msg=FORBIDDEN, count=3),
    _feed(5, "Locked Out", msg=AUTH, count=5),
    _feed(6, "Gone Quiet Recently"),
    _feed(7, "Gone Quiet Long Ago"),
    _feed(8, "Perfectly Fine"),
    _feed(9, "Resting", disabled=True),
]

# feed_id -> (days since newest entry, median gap in hours)
CADENCE = {
    1: (1, 6), 2: (1, 6), 3: (1, 6), 4: (1, 6), 5: (1, 6),
    6: (3, 1),        # hourly publisher, silent three days -> quiet
    7: (800, 24),     # daily publisher, silent since forever -> quiet
    8: (0, 24),       # daily publisher, posted today -> fine
    9: (400, 24),
}


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    async def execute(self, sql, params=None):
        if "percentile_cont" in sql:
            return _Cursor([
                {"feed_id": fid,
                 "latest": NOW - timedelta(days=days),
                 "median_gap_s": float(gap_h * 3600)}
                for fid, (days, gap_h) in CADENCE.items()
            ])
        if "count(*)" in sql.lower() and "group by" not in sql.lower():
            return _Cursor([{"n": 0, "cnt": 0}])
        return _Cursor([])

    async def commit(self):
        pass


@contextlib.asynccontextmanager
async def _fake_get_conn():
    yield _Conn()


@pytest.fixture
def client(monkeypatch):
    async def get_feeds():
        return [dict(f) for f in FEEDS]

    async def get_feed_counters():
        return {"unreads": {}, "reads": {}}

    async def get_entries(**kw):
        return {"total": 0, "entries": []}

    async def get_categories():
        return [{"id": 1, "title": "News"}]

    for name, fn in [("get_feeds", get_feeds), ("get_feed_counters", get_feed_counters),
                     ("get_entries", get_entries), ("get_categories", get_categories)]:
        monkeypatch.setattr(miniflux_client, name, fn)
    for mod in (entries_routes, feeds_routes, cookies_routes, cadence):
        monkeypatch.setattr(mod, "get_conn", _fake_get_conn)

    async def score(articles):
        return None

    async def embed_sims(conn, entry_ids):
        return {}

    async def has_credentials(domain):
        return False

    monkeypatch.setattr(ranker_client, "score", score)
    monkeypatch.setattr(embeddings, "embed_sims", embed_sims)
    monkeypatch.setattr(credvault, "has_credentials", has_credentials)
    entries_routes._invalidate_sidebar_cache()
    cadence.invalidate()
    return TestClient(app)


@pytest.fixture
def all_healthy(monkeypatch, client):
    async def get_feeds():
        return [dict(_feed(8, "Perfectly Fine"))]

    monkeypatch.setattr(miniflux_client, "get_feeds", get_feeds)
    entries_routes._invalidate_sidebar_cache()
    cadence.invalidate()
    return client


