"""Broken feeds, grouped by what is wrong with them rather than listed one by one.

The feeds page answers "which feeds are unwell". This answers the next
question — "what do I do about it" — and it answers it per cause, because a
cause is the unit the fix applies to. Nine feeds behind one Cloudflare rule are
one decision, not nine.

Laid out in the reader's own three-pane shell: causes in the list column,
the focused cause and its remedies in the reader column.
"""
import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import miniflux_client
from app.cadence import all_feeds as feed_cadence
from app.remedies import attention_count, group_by_cause, recency_first
from app.routes.entries import build_sidebar
from app.routes.feeds import _fetch_feed_configs, decorate_feeds
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


async def _groups() -> tuple[list, int]:
    """Every cause with feeds in it right now, worst and largest first, and the
    count of feeds that actually need answering.

    The two differ, and deliberately: paused feeds are listed — you may well
    want to resume a batch — but they are not a problem anyone has to solve, so
    counting them in the headline would make it permanently alarming.
    """
    feeds, counters, cadence, configs = await asyncio.gather(
        miniflux_client.get_feeds(),
        miniflux_client.get_feed_counters(),
        feed_cadence(),
        _fetch_feed_configs(),
    )
    feeds = decorate_feeds(feeds, unreads=counters.get("unreads", {}),
                           cadence=cadence, configs=configs, now=datetime.now(UTC))
    return group_by_cause(feeds, key=recency_first), attention_count(feeds)


def _wants_reader_fragment(request: Request) -> bool:
    """True when HTMX is swapping only the reader pane, as the list rows do."""
    return (request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-Target") == "reader-col")


async def _render(request: Request, groups, focused, total, *, fragment: bool):
    if fragment:
        return templates.TemplateResponse(request, "_triage_cause.html",
                                          {"cause": focused})
    return templates.TemplateResponse(request, "triage.html", {
        "groups": groups,
        "cause": focused,
        "total": total,
        # Not a VIEW_TITLES entry: triage is not an entries view, and adding it
        # there would make `/entries?view=triage` resolve to an unfiltered list.
        "sidebar": {**await build_sidebar(active_view=None, active_feed_id=None),
                    "view": "triage", "view_title": "Needs attention"},
    })


@router.get("/triage", response_class=HTMLResponse)
async def triage(request: Request):
    groups, total = await _groups()
    # Open on the worst group rather than an empty pane: there is always a
    # most-pressing cause, and making the reader click to find it is a step
    # that never has a different answer.
    return await _render(request, groups, groups[0] if groups else None, total,
                         fragment=False)


@router.get("/triage/{bucket}", response_class=HTMLResponse)
async def triage_cause(request: Request, bucket: str):
    groups, total = await _groups()
    focused = next((g for g in groups if g.bucket == bucket), None)
    if focused is None:
        # A real bucket with nothing in it is as gone as one that never
        # existed; an empty remedy pane would be worse than a 404.
        raise HTTPException(status_code=404, detail=f"No feeds are {bucket!r} right now")
    return await _render(request, groups, focused, total,
                         fragment=_wants_reader_fragment(request))
