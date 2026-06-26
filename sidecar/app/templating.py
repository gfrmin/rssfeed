from datetime import datetime, timezone
from pathlib import Path

import humanize
import nh3
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# --- HTML sanitisation (the trust boundary for feed/web-supplied HTML) ----------
# Autoescape is on, so by default any markup in a value is escaped (shown as text).
# These filters opt specific values back into rendered HTML *after* sanitising with
# nh3 (Rust/ammonia): strict for titles, richer for article bodies. They return
# Markup so Jinja won't re-escape. nh3 strips event handlers, javascript: URLs,
# <script>/<iframe>, and any tag/attribute not on the allowlist.

# Titles: inline formatting only — no links, no attributes, no block/media tags.
_TITLE_TAGS = {"i", "em", "b", "strong", "sub", "sup", "code", "mark",
               "u", "s", "small", "abbr", "span", "br"}

# Article bodies: structural + inline + media, but no <iframe> and no event
# handlers. URL attributes are scheme-restricted; relative URLs (the /proxy/image
# rewrite) pass through unchanged.
_BODY_TAGS = {
    "p", "div", "span", "br", "a", "em", "i", "b", "strong", "u", "s",
    "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "figure", "figcaption", "img", "table", "thead", "tbody", "tr", "td", "th",
    "pre", "code", "sup", "sub", "video", "audio", "source",
}
_BODY_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "video": {"src", "controls", "poster", "width", "height"},
    "audio": {"src", "controls"},
    "source": {"src", "type"},
}
_URL_SCHEMES = {"http", "https", "mailto"}


def _inline_html(value) -> Markup:
    """Sanitise a title to inline formatting only, preserving e.g. <i>…</i>."""
    if not value:
        return Markup("")
    return Markup(nh3.clean(str(value), tags=_TITLE_TAGS, attributes={}))


def _clean_body(value) -> Markup:
    """Sanitise article-body HTML for rendering (replaces a raw `| safe`)."""
    if not value:
        return Markup("")
    return Markup(nh3.clean(
        str(value), tags=_BODY_TAGS, attributes=_BODY_ATTRS,
        url_schemes=_URL_SCHEMES,
    ))


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


templates.env.filters["timeago"] = _timeago
templates.env.filters["reading_time"] = _reading_time
templates.env.filters["excerpt"] = _excerpt
templates.env.filters["inline_html"] = _inline_html
templates.env.filters["clean_body"] = _clean_body
