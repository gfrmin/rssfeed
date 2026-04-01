import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

import psycopg.types.json

from app.db import get_conn
from app.templating import templates

router = APIRouter()


@router.get("/filters", response_class=HTMLResponse)
async def filter_list(request: Request):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT * FROM saved_filters ORDER BY created_at DESC"
        )
        filters = await cur.fetchall()
    return templates.TemplateResponse(
        request, "filters.html", {"filters": filters}
    )


@router.post("/filters")
async def create_filter(
    name: str = Form(...),
    rules_json: str = Form("[]"),
    auto_action: str = Form(""),
):
    try:
        rules = json.loads(rules_json)
    except json.JSONDecodeError as e:
        return HTMLResponse(f'<span class="error">Invalid JSON: {e}</span>', status_code=400)

    async with get_conn() as conn:
        await conn.execute(
            "INSERT INTO saved_filters (name, rules, auto_action) VALUES (%s, %s::jsonb, %s)",
            (name, psycopg.types.json.Json(rules), auto_action or None),
        )
        await conn.commit()
    return HTMLResponse(
        '<span class="success">Filter created</span>',
        headers={"HX-Redirect": "/filters"},
    )


@router.post("/filters/{filter_id}/delete")
async def delete_filter(filter_id: int):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM saved_filters WHERE id = %s", (filter_id,))
        await conn.commit()
    return HTMLResponse(
        '<span class="success">Deleted</span>',
        headers={"HX-Redirect": "/filters"},
    )


def matches_rules(entry: dict, rules: list[dict]) -> bool:
    """Check if an entry matches all filter rules."""
    for rule in rules:
        field = rule.get("field", "")
        op = rule.get("op", "")
        value = rule.get("value", "")

        entry_value = ""
        if field == "title":
            entry_value = entry.get("title", "")
        elif field == "content":
            entry_value = entry.get("content", "")
        elif field == "author":
            entry_value = entry.get("author", "")
        elif field == "url":
            entry_value = entry.get("url", "")
        elif field == "feed_title":
            entry_value = entry.get("feed", {}).get("title", "")

        if op == "contains" and value.lower() not in entry_value.lower():
            return False
        elif op == "not_contains" and value.lower() in entry_value.lower():
            return False
        elif op == "equals" and entry_value.lower() != value.lower():
            return False
        elif op == "starts_with" and not entry_value.lower().startswith(value.lower()):
            return False

    return True
