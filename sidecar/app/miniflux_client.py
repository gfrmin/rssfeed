import asyncio
import time
from typing import Any

import httpx

from app.config import MINIFLUX_API_KEY, MINIFLUX_URL


_client: httpx.AsyncClient | None = None

_FEEDS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_FEEDS_CACHE_TTL = 10.0
_FEEDS_CACHE_LOCK = asyncio.Lock()


async def startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=MINIFLUX_URL,
        headers={"X-Auth-Token": MINIFLUX_API_KEY},
        timeout=30.0,
    )


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("miniflux_client not started — startup() was not called")
    return _client


def _invalidate_feeds_cache() -> None:
    _FEEDS_CACHE.pop("feeds", None)


async def get_feeds() -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _FEEDS_CACHE.get("feeds")
    if cached and now - cached[0] < _FEEDS_CACHE_TTL:
        return cached[1]
    async with _FEEDS_CACHE_LOCK:
        cached = _FEEDS_CACHE.get("feeds")
        if cached and time.monotonic() - cached[0] < _FEEDS_CACHE_TTL:
            return cached[1]
        r = await _get().get("/v1/feeds")
        r.raise_for_status()
        data = r.json()
        _FEEDS_CACHE["feeds"] = (time.monotonic(), data)
        return data


async def get_feed(feed_id: int) -> dict[str, Any]:
    r = await _get().get(f"/v1/feeds/{feed_id}")
    r.raise_for_status()
    return r.json()


async def get_entries(
    *,
    feed_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    direction: str = "desc",
    order: str = "published_at",
    search: str | None = None,
    starred: bool = False,
    category_id: int | None = None,
    after: str | None = None,
    before: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
        "direction": direction,
        "order": order,
    }
    if status:
        params["status"] = status
    if search:
        params["search"] = search
    if starred:
        params["starred"] = "true"
    if category_id:
        params["category_id"] = category_id
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    path = f"/v1/feeds/{feed_id}/entries" if feed_id else "/v1/entries"
    r = await _get().get(path, params=params)
    r.raise_for_status()
    return r.json()


async def get_entry(entry_id: int) -> dict[str, Any]:
    r = await _get().get(f"/v1/entries/{entry_id}")
    r.raise_for_status()
    return r.json()


async def update_entry_status(entry_ids: list[int], status: str) -> None:
    r = await _get().put(
        "/v1/entries",
        json={"entry_ids": entry_ids, "status": status},
    )
    r.raise_for_status()


async def toggle_bookmark(entry_id: int) -> None:
    r = await _get().put(f"/v1/entries/{entry_id}/bookmark")
    r.raise_for_status()


async def get_categories() -> list[dict[str, Any]]:
    r = await _get().get("/v1/categories")
    r.raise_for_status()
    return r.json()


async def get_feed_counters() -> dict[str, Any]:
    r = await _get().get("/v1/feeds/counters")
    r.raise_for_status()
    return r.json()


async def export_opml() -> str:
    r = await _get().get("/v1/export")
    r.raise_for_status()
    return r.text


async def create_feed(feed_url: str, category_id: int) -> dict[str, Any]:
    r = await _get().post("/v1/feeds", json={"feed_url": feed_url, "category_id": category_id})
    r.raise_for_status()
    _invalidate_feeds_cache()
    return r.json()


async def discover(url: str) -> list[dict[str, Any]]:
    """Probe a URL via Miniflux's /v1/discover. Returns [{url, title, type}, ...]."""
    r = await _get().post("/v1/discover", json={"url": url})
    r.raise_for_status()
    return r.json()


async def delete_feed(feed_id: int) -> None:
    r = await _get().delete(f"/v1/feeds/{feed_id}")
    r.raise_for_status()
    _invalidate_feeds_cache()


async def refresh_feed(feed_id: int) -> None:
    r = await _get().put(f"/v1/feeds/{feed_id}/refresh")
    r.raise_for_status()


async def update_feed(feed_id: int, **fields: Any) -> dict[str, Any]:
    r = await _get().put(f"/v1/feeds/{feed_id}", json=fields)
    r.raise_for_status()
    _invalidate_feeds_cache()
    return r.json()


async def import_opml(data: bytes) -> None:
    r = await _get().post(
        "/v1/import",
        content=data,
        headers={"Content-Type": "application/xml"},
    )
    r.raise_for_status()
    _invalidate_feeds_cache()
