"""Saved searches — pinned sidebar entries that re-apply a query + tag filter.

Distinct from saved_filters (which auto-act at fetch time); these are just
shortcuts to a filtered list view. Persisted so they survive reloads.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

import psycopg.types.json

from app.db import get_conn
from app.templating import templates
from app.routes.entries import build_sidebar

router = APIRouter()


async def _sidebar_oob(request: Request, active_view: str) -> HTMLResponse:
    """Re-render the sidebar as an out-of-band swap (preserves the open reader)."""
    sidebar = await build_sidebar(active_view=active_view or None, active_feed_id=None)
    return templates.TemplateResponse(request, "_sidebar_oob.html", {"sidebar": sidebar})


@router.post("/searches")
async def create_search(
    request: Request,
    name: str = Form(""),
    search: str = Form(""),
    tag: list[str] = Form(default=[]),
    view: str = Form(""),
    icon: str = Form("search"),
):
    label = name.strip() or search.strip() or (", ".join(tag) if tag else "Saved search")
    async with get_conn() as conn:
        await conn.execute(
            "INSERT INTO saved_searches (name, icon, query, tags, view) "
            "VALUES (%s, %s, %s, %s::jsonb, %s)",
            (label, icon or "search", search.strip(), psycopg.types.json.Json(tag), view or None),
        )
        await conn.commit()
    return await _sidebar_oob(request, view)


@router.post("/searches/{search_id}/delete")
async def delete_search(request: Request, search_id: int, view: str = Form("")):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM saved_searches WHERE id = %s", (search_id,))
        await conn.commit()
    return await _sidebar_oob(request, view)
