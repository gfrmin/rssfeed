from datetime import datetime, timezone
from pathlib import Path

import humanize
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _timeago(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.total_seconds() < 86400:
            return humanize.naturaltime(delta)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16].replace("T", " ")


def _reading_time(content: str) -> str:
    if not content:
        return "< 1 min"
    words = len(content.split())
    minutes = max(1, words // 230)
    return f"{minutes} min read"


def _excerpt(content: str, length: int = 200) -> str:
    if not content:
        return ""
    import re
    text = re.sub(r"<[^>]+>", "", content)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


templates.env.filters["timeago"] = _timeago
templates.env.filters["reading_time"] = _reading_time
templates.env.filters["excerpt"] = _excerpt
