"""Endpoint smoke tests — the whole app driven through Starlette's TestClient.

Hermetic: no Postgres, no Miniflux, no network. TestClient is deliberately NOT
used as a context manager, so the lifespan (migrations, Miniflux client,
worker loop, ranker warmup) never runs; Miniflux calls and DB connections are
monkeypatched instead. The fake DB returns empty result sets, which every
query site already treats as "no config / no snapshots".
"""
import contextlib

import pytest
from fastapi.testclient import TestClient

from app import cadence, credvault, egress, embeddings, miniflux_client, ranker_client
from app.main import app
from app.routes import cookies as cookies_routes
from app.routes import entries as entries_routes
from app.routes import feeds as feeds_routes

FEED = {
    "id": 1, "title": "Example Feed", "site_url": "https://feed.example/",
    "feed_url": "https://feed.example/rss", "checked_at": "2026-01-01T00:00:00Z",
    "parsing_error_count": 0, "parsing_error_message": "", "disabled": False,
    "category": {"id": 1, "title": "News"},
}
ENTRY = {
    "id": 101, "feed_id": 1, "title": "Hello world",
    "url": "https://feed.example/post", "author": "", "content": "<p>hi</p>",
    "status": "unread", "starred": False, "published_at": "2026-01-01T00:00:00Z",
    "tags": [], "enclosures": [], "feed": FEED,
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
        # Scalar count queries must return a row (keys cover both aliases in
        # use); GROUP BY queries that also mention COUNT(*) (e.g. the
        # multi-snapshot lookups in routes/entries.py) return per-row results
        # keyed by their own columns, not a bare count, so they must fall
        # through to the empty case like everything else — no feed_config,
        # snapshots, cookies.
        sql_lower = sql.lower()
        if "count(*)" in sql_lower and "group by" not in sql_lower:
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
        return [dict(FEED)]

    async def get_feed(feed_id):
        return dict(FEED)

    async def get_feed_counters():
        return {"unreads": {"1": 1}, "reads": {"1": 2}}

    async def get_entries(**kw):
        return {"total": 1, "entries": [dict(ENTRY)]}

    async def get_entry(entry_id):
        return dict(ENTRY)

    async def get_categories():
        return [{"id": 1, "title": "News"}]

    async def update_entry_status(entry_ids, status):
        return None

    for name, fn in [
        ("get_feeds", get_feeds), ("get_feed", get_feed),
        ("get_feed_counters", get_feed_counters), ("get_entries", get_entries),
        ("get_entry", get_entry), ("get_categories", get_categories),
        ("update_entry_status", update_entry_status),
    ]:
        monkeypatch.setattr(miniflux_client, name, fn)

    for mod in (entries_routes, feeds_routes, cookies_routes, cadence):
        monkeypatch.setattr(mod, "get_conn", _fake_get_conn)

    async def score(articles):
        return None  # ranker unavailable — priority+recency fallback

    async def embed_sims(conn, entry_ids):
        return {}

    async def has_credentials(domain):
        return False

    monkeypatch.setattr(ranker_client, "score", score)
    monkeypatch.setattr(embeddings, "embed_sims", embed_sims)
    monkeypatch.setattr(credvault, "has_credentials", has_credentials)
    entries_routes._invalidate_sidebar_cache()
    cadence.invalidate()
    # No context manager: entering it would run the lifespan (DB migrations,
    # Miniflux startup, worker loop) — exactly what these tests must avoid.
    return TestClient(app)


def test_entries_list(client):
    r = client.get("/entries")
    assert r.status_code == 200
    assert "Hello world" in r.text


def test_entries_list_fragment(client):
    r = client.get("/entries", headers={"HX-Request": "true", "HX-Target": "list-col"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()   # fragment, not the full shell


def test_feed_entry_list(client):
    r = client.get("/entries", params={"feed_id": 1})
    assert r.status_code == 200


def test_feeds_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Example Feed" in r.text


def test_entry_detail_full_shell(client):
    r = client.get("/entries/101")
    assert r.status_code == 200
    assert "Hello world" in r.text


def test_entry_detail_reader_fragment(client):
    r = client.get("/entries/101", headers={"HX-Request": "true", "HX-Target": "reader-col"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()


def test_feed_settings_page(client):
    r = client.get("/feeds/1")
    assert r.status_code == 200


def test_new_count(client):
    r = client.get("/api/new-count")
    assert r.status_code == 200
    assert r.json() == {"count": 1}


def test_404_html_page(client):
    r = client.get("/definitely-not-a-page", headers={"accept": "text/html"})
    assert r.status_code == 404


def test_404_json(client):
    r = client.get("/api/definitely-not", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert r.json()["detail"]


# --- /proxy/image egress rejections (guard lives in app/egress.py) ----------

@pytest.mark.parametrize("target", [
    "file:///etc/passwd",
    "http://127.0.0.1/x.png",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/x.png",
])
def test_proxy_image_blocked(client, target):
    r = client.get("/proxy/image", params={"url": target})
    assert r.status_code == 403


def test_proxy_image_blocked_via_dns(client, monkeypatch):
    async def resolve(host, port):
        return ["10.0.0.5"]
    monkeypatch.setattr(egress, "_resolve", resolve)
    r = client.get("/proxy/image", params={"url": "http://internal.example/a.png"})
    assert r.status_code == 403
