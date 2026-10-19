"""Serve the invented publications in content.py: feeds, article pages, images.

    python seed/server.py [--port 9148]

Stdlib only. Dates are relative to server start, so "3h ago" stays "3h ago"
for a run. POST /_break turns on each feed's `fail` mode (500, 403, 404, an
HTML page instead of XML); seed.py calls it after subscribing, because
Miniflux refuses to subscribe to a feed that is already broken.
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from content import (  # noqa: E402
    CHANGING,
    FEEDS,
    Feed,
    author,
    by_slug,
    entry_tags,
    entry_title,
    paragraphs,
    published,
)

NOW = datetime.now(UTC).replace(microsecond=0)
STATE = {"broken": False, "fetches": {}}


def base(handler) -> str:
    return f"http://{handler.headers.get('Host', '127.0.0.1')}"


def article_body(feed: Feed, i: int, root: str, version: int = 1) -> str:
    paras = paragraphs(feed, i)
    if version > 1:
        paras = [paras[0].replace("Tuesday", "Wednesday"), *paras[1:]]
        paras.append("Correction: an earlier version of this article gave the wrong day "
                     "for the meeting. It was a Wednesday.")
    img = (f'<figure><img src="{root}/img/{feed.slug}/{i}.svg" alt="A sketch for {html.escape(entry_title(feed, i))}" '
           'width="640" height="360"><figcaption>Illustration: the seed server.</figcaption></figure>'
           if feed.image else "")
    link = f'<p>Background: <a href="{root}/articles/{feed.slug}/{i + 1}.html">the previous instalment</a>.</p>'
    quote = f"<blockquote><p>{html.escape(paras[1].split('.')[0])}.</p></blockquote>"
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paras[:2]) + img + quote
    body += "".join(f"<p>{html.escape(p)}</p>" for p in paras[2:]) + link
    return body


def teaser(feed: Feed, i: int) -> str:
    return html.escape(paragraphs(feed, i)[0].split(". ")[0]) + "."


def rss(feed: Feed, root: str) -> str:
    def item(i: int) -> str:
        url = f"{root}/articles/{feed.slug}/{i}.html"
        desc = f"<p>{teaser(feed, i)}</p>" if feed.teaser else article_body(feed, i, root)
        cats = "".join(f"<category>{html.escape(t)}</category>" for t in entry_tags(feed, i))
        enc = (f'<enclosure url="{root}/audio/{feed.slug}/{i}.mp3" length="1024" type="audio/mpeg"/>'
               if feed.audio else "")
        return (f"<item><title>{html.escape(entry_title(feed, i))}</title><link>{url}</link>"
                f"<guid isPermaLink=\"true\">{url}</guid>"
                f"<pubDate>{format_datetime(published(feed, i, NOW))}</pubDate>"
                f"<dc:creator>{html.escape(author(feed, i))}</dc:creator>{cats}{enc}"
                f"<description><![CDATA[{desc}]]></description></item>")

    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            f"<title>{html.escape(feed.title)}</title><link>{root}/{feed.slug}/</link>"
            f"<language>{feed.lang}</language>"
            f"<description>Invented publication for the rssfeed seed.</description>"
            + "".join(item(i) for i in range(feed.count)) + "</channel></rss>")


def page(feed: Feed, title: str, body: str, root: str) -> str:
    d = ' dir="rtl"' if feed.lang == "he" else ""
    return (f'<!doctype html><html lang="{feed.lang}"{d}><head><meta charset="utf-8">'
            f"<title>{html.escape(title)} — {html.escape(feed.title)}</title>"
            f'<link rel="alternate" type="application/rss+xml" title="{html.escape(feed.title)}" '
            f'href="{root}/feeds/{feed.slug}.xml"></head>'
            f"<body><header><a href=\"{root}/{feed.slug}/\">{html.escape(feed.title)}</a></header>"
            f"<main>{body}</main></body></html>")


def article_page(feed: Feed, i: int, root: str) -> str:
    key = (feed.slug, i)
    n = STATE["fetches"][key] = STATE["fetches"].get(key, 0) + 1
    version = 2 if key == CHANGING and n > 1 else 1
    if feed.paywall:
        body = f"<p>{teaser(feed, i)}</p><p class=\"paywall\">Subscribe to keep reading.</p>"
    else:
        body = article_body(feed, i, root, version)
    title = entry_title(feed, i)
    return page(feed, title, f"<article><h1>{html.escape(title)}</h1>"
                f"<p class=\"byline\">By {html.escape(author(feed, i))}</p>{body}</article>", root)


def svg(slug: str, i: int) -> str:
    hue = (len(slug) * 47 + i * 31) % 360
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
            f'<rect width="640" height="360" fill="hsl({hue},35%,70%)"/>'
            f'<circle cx="{120 + i * 40 % 400}" cy="180" r="90" fill="hsl({hue},45%,45%)"/></svg>')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet unless asked
        if self.server.verbose:
            super().log_message(fmt, *args)

    def send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/_break":
            STATE["broken"] = True
            return self.send(204, b"")
        self.send(404, "no")

    def do_GET(self):
        root, parts = base(self), [p for p in self.path.split("?")[0].split("/") if p]
        try:
            if len(parts) == 2 and parts[0] == "feeds" and parts[1].endswith(".xml"):
                return self.feed(by_slug(parts[1][:-4]), root)
            if len(parts) == 3 and parts[0] == "articles":
                return self.send(200, article_page(by_slug(parts[1]), int(parts[2].split(".")[0]), root))
            if len(parts) == 3 and parts[0] == "img":
                return self.send(200, svg(parts[1], int(parts[2].split(".")[0])), "image/svg+xml")
            if len(parts) == 3 and parts[0] == "audio":
                return self.send(200, b"\0" * 1024, "audio/mpeg")
            if len(parts) == 1:
                f = by_slug(parts[0])
                return self.send(200, page(f, "Home", "<p>Welcome.</p>", root))
        except (StopIteration, ValueError):
            pass
        self.send(404, "not found")

    def feed(self, f: Feed, root: str):
        mode = f.fail if STATE["broken"] else ""
        if mode == "500":
            return self.send(500, "server error")
        if mode == "403":
            return self.send(403, "forbidden")
        if mode == "404":
            return self.send(404, "gone")
        if mode == "html":
            return self.send(200, page(f, "Home", "<p>We moved to a newsletter.</p>", root))
        self.send(200, rss(f, root), "application/rss+xml; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9148)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.verbose = a.v
    print(f"seed server on http://127.0.0.1:{a.port} ({len(FEEDS)} feeds)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
