from typing import Any

import httpx

from app.config import MINIFLUX_API_KEY, MINIFLUX_URL


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=MINIFLUX_URL,
        headers={"X-Auth-Token": MINIFLUX_API_KEY},
        timeout=30.0,
    )


async def get_feeds() -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get("/v1/feeds")
        r.raise_for_status()
        return r.json()


async def get_feed(feed_id: int) -> dict[str, Any]:
    async with _client() as c:
        r = await c.get(f"/v1/feeds/{feed_id}")
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
    async with _client() as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def get_entry(entry_id: int) -> dict[str, Any]:
    async with _client() as c:
        r = await c.get(f"/v1/entries/{entry_id}")
        r.raise_for_status()
        return r.json()


async def update_entry_status(entry_ids: list[int], status: str) -> None:
    async with _client() as c:
        r = await c.put(
            "/v1/entries",
            json={"entry_ids": entry_ids, "status": status},
        )
        r.raise_for_status()


async def toggle_bookmark(entry_id: int) -> None:
    async with _client() as c:
        r = await c.put(f"/v1/entries/{entry_id}/bookmark")
        r.raise_for_status()


async def get_categories() -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get("/v1/categories")
        r.raise_for_status()
        return r.json()


async def get_feed_counters() -> dict[str, Any]:
    async with _client() as c:
        r = await c.get("/v1/feeds/counters")
        r.raise_for_status()
        return r.json()


async def get_feed_icon(feed_id: int) -> dict[str, Any] | None:
    async with _client() as c:
        try:
            r = await c.get(f"/v1/feeds/{feed_id}/icon")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError:
            return None


async def export_opml() -> str:
    async with _client() as c:
        r = await c.get("/v1/export")
        r.raise_for_status()
        return r.text


async def import_opml(data: bytes) -> None:
    async with _client() as c:
        r = await c.post(
            "/v1/import",
            content=data,
            headers={"Content-Type": "application/xml"},
        )
        r.raise_for_status()
