import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import humanize
import nh3
from fastapi.templating import Jinja2Templates
from lxml import html as lxml_html
from markupsafe import Markup

from app import config

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Cache-busting query string for static assets, derived from their content so it
# changes automatically on every edit — no more hand-bumped "?v=ds7" going stale
# and leaving browsers on an old stylesheet. Computed once at startup.
_STATIC_DIR = Path(__file__).parent.parent / "static"


def _asset_version() -> str:
    h = hashlib.sha1()
    for name in ("style.css", "tailwind.css"):
        try:
            h.update((_STATIC_DIR / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()

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
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title"},
    "video": {"src", "controls", "poster", "width", "height"},
    "audio": {"src", "controls"},
    "source": {"src", "type"},
}
_URL_SCHEMES = {"http", "https", "mailto"}

# --- Embeds: convert <iframe> to click-through links ----------------------------
# We never render inline third-party frames (nh3 strips <iframe> anyway). The
# extractor preserves the embed in content_html; here, at render, each iframe
# becomes a plain link the reader chooses to follow — YouTube routed to the
# configured Invidious instance (or youtube.com), other embeds to their source.
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                  "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}


def _youtube_id(src: str) -> str | None:
    """Video id from a YouTube embed/watch/short URL, else None (not YouTube)."""
    try:
        u = urlparse(src)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif "/embed/" in u.path:
        vid = u.path.split("/embed/", 1)[1].split("/")[0]
    elif u.path.rstrip("/") == "/watch":
        vid = parse_qs(u.query).get("v", [""])[0]
    else:
        return None
    return vid.strip() or None


def _embed_anchor(src: str) -> lxml_html.HtmlElement | None:
    """Build the click-through <a> for an embed src, or None to drop it."""
    src = (src or "").strip()
    if urlparse(src).scheme not in ("http", "https"):
        return None
    a = lxml_html.Element("a")
    a.set("target", "_blank")
    vid = _youtube_id(src)
    if vid:
        base = config.INVIDIOUS_URL or "https://www.youtube.com"
        a.set("href", f"{base}/watch?v={vid}")
        a.text = "▶ Watch on Invidious" if config.INVIDIOUS_URL else "▶ Watch on YouTube"
    else:
        a.set("href", src)
        a.text = "↗ Open embedded content"
    return a


def _render_embeds(html: str) -> str:
    """Replace every <iframe> with a click-through link (must run before nh3,
    which strips iframes outright). Frames with a non-web src are dropped."""
    if "<iframe" not in html.lower():
        return html
    try:
        root = lxml_html.fromstring(f"<div>{html}</div>")
    except Exception:
        return html
    for ifr in root.xpath("//iframe"):
        parent = ifr.getparent()
        if parent is None:
            continue
        anchor = _embed_anchor(ifr.get("src") or "")
        if anchor is None:
            parent.remove(ifr)
        else:
            anchor.tail = ifr.tail  # keep any text that followed the frame
            parent.replace(ifr, anchor)
    parts = [root.text or ""]
    for child in root:
        parts.append(lxml_html.tostring(child, encoding="unicode"))
    return "".join(parts)


def _inline_html(value) -> Markup:
    """Sanitise a title to inline formatting only, preserving e.g. <i>…</i>."""
    if not value:
        return Markup("")
    return Markup(nh3.clean(str(value), tags=_TITLE_TAGS, attributes={}))


def _clean_body(value) -> Markup:
    """Sanitise article-body HTML for rendering (replaces a raw `| safe`).

    Embeds are turned into click-through links first (nh3 would otherwise strip the
    <iframe> and leave nothing); rel=noopener is forced on every link for safety."""
    if not value:
        return Markup("")
    return Markup(nh3.clean(
        _render_embeds(str(value)), tags=_BODY_TAGS, attributes=_BODY_ATTRS,
        url_schemes=_URL_SCHEMES, link_rel="noopener noreferrer",
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
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = now - dt
        if delta.total_seconds() < 86400:
            return humanize.naturaltime(delta)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)[:16].replace("T", " ")


def _duration(seconds) -> str:
    """A span of time, short enough to sit at the end of a dense row.

    Coarser as it gets longer, because that is how the value is read: the
    difference between 7h and 8h matters, the difference between 800 and 830
    days does not.
    """
    if seconds is None or seconds == "":
        return ""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    if s < 86400:
        return f"{int(s // 3600)}h"
    days = int(s // 86400)
    if days < 90:
        return f"{days}d"
    if days < 365:
        # capped: "12mo" is a year, and the next branch already says so
        return f"{min(11, days // 30)}mo"
    return f"{days // 365}y"


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
templates.env.filters["duration"] = _duration
templates.env.filters["reading_time"] = _reading_time
templates.env.filters["excerpt"] = _excerpt
templates.env.filters["inline_html"] = _inline_html
templates.env.filters["clean_body"] = _clean_body
