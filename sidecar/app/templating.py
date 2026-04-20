from datetime import datetime, timezone
from pathlib import Path

import humanize
import markdown as _markdown
from markupsafe import Markup
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _timeago(value) -> str:
    if not value:
        return ""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.total_seconds() < 86400:
            return humanize.naturaltime(delta)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)[:16].replace("T", " ")


def _reading_time(content: str) -> str:
    if not content:
        return "< 1 min"
    words = len(content.split())
    minutes = max(1, words // 230)
    return f"{minutes} min read"


def _excerpt(content: str, length: int = 400) -> str:
    if not content:
        return ""
    import re
    from html import unescape
    text = re.sub(r"<[^>]+>", "", content)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def _md(text: str) -> Markup:
    """Render LLM-produced markdown to safe-ish HTML. Escapes raw HTML in the source."""
    if not text:
        return Markup("")
    html = _markdown.markdown(
        text,
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html",
    )
    return Markup(html)


templates.env.filters["timeago"] = _timeago
templates.env.filters["reading_time"] = _reading_time
templates.env.filters["excerpt"] = _excerpt
templates.env.filters["md"] = _md
