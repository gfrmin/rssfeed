"""Turning the proxy off has to actually stop proxied polling.

Miniflux picks a feed's own `proxy_url` whenever it is non-empty, before it
ever looks at `fetch_via_proxy` -- that flag only selects the app-wide proxy
(`internal/reader/fetcher/request_builder.go`, 2.3.3). So "Proxy off" that only
flips the flag leaves the feed polling through BrightData, still metered, while
the UI says OFF. Clearing the URL is what turns it off; Miniflux accepts an
empty `proxy_url` and stores it.
"""
import pytest

from app import miniflux_client
from app.routes import feeds as feeds_routes


@pytest.fixture
def updates(monkeypatch, client):
    calls = []

    async def update_feed(feed_id, **fields):
        calls.append((feed_id, fields))
        return {}

    async def get_feed(feed_id):
        return {"id": feed_id, "fetch_via_proxy": True, "proxy_url": "http://proxy"}

    async def refresh_feed(feed_id):
        return None

    monkeypatch.setattr(miniflux_client, "update_feed", update_feed)
    monkeypatch.setattr(miniflux_client, "get_feed", get_feed)
    monkeypatch.setattr(miniflux_client, "refresh_feed", refresh_feed)
    monkeypatch.setattr(feeds_routes, "BRIGHTDATA_PROXY", "http://proxy")
    return calls


def test_bulk_proxy_off_clears_the_stored_proxy_url(client, updates):
    r = client.post("/feeds/unset-proxy", data={"feed_ids": ["3", "4"]})
    assert r.status_code == 200
    assert sorted(fid for fid, _ in updates) == [3, 4]
    for _, fields in updates:
        assert fields == {"fetch_via_proxy": False, "proxy_url": ""}


def test_toggling_the_proxy_off_clears_the_stored_proxy_url(client, updates):
    r = client.post("/feeds/7/toggle-proxy")
    assert r.status_code == 200
    assert updates == [(7, {"fetch_via_proxy": False, "proxy_url": ""})]


def test_toggling_the_proxy_on_still_stores_the_proxy_url(client, updates, monkeypatch):
    async def get_feed(feed_id):
        return {"id": feed_id, "fetch_via_proxy": False, "proxy_url": ""}

    monkeypatch.setattr(miniflux_client, "get_feed", get_feed)
    client.post("/feeds/7/toggle-proxy")
    assert updates == [(7, {"proxy_url": "http://proxy", "fetch_via_proxy": True})]
