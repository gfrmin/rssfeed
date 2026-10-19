"""The seeded instance's invented content stays loadable and deterministic.

seed/ is what the UX loop audits and crops from (the repo is public, so it
cannot crop the real instance). If its feeds stop parsing, every baseline
quietly measures an emptier app.
"""
import sys
import xml.dom.minidom
from datetime import UTC, datetime
from pathlib import Path

SEED = Path(__file__).resolve().parents[2] / "seed"
sys.path.insert(0, str(SEED))

import content  # noqa: E402
import server  # noqa: E402


def test_every_seed_feed_renders_as_parseable_rss():
    for feed in content.FEEDS:
        doc = xml.dom.minidom.parseString(server.rss(feed, "http://127.0.0.1:9148"))
        assert len(doc.getElementsByTagName("item")) == feed.count, feed.slug


def test_seed_content_is_deterministic():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    feed = content.by_slug("tidewater")
    assert content.entry_title(feed, 3) == content.entry_title(feed, 3)
    assert content.paragraphs(feed, 3) == content.paragraphs(feed, 3)
    assert content.published(feed, 0, now) > content.published(feed, 1, now)


def test_every_triage_cause_the_seed_promises_has_a_feed():
    assert {f.fail for f in content.FEEDS} - {""} == {"500", "403", "404", "html", "refused", "dns"}
    assert any(f.paused for f in content.FEEDS)
    assert any(f.silent_for for f in content.FEEDS)


def test_the_changing_article_differs_between_fetches():
    slug, i = content.CHANGING
    feed = content.by_slug(slug)
    root = "http://127.0.0.1:9148"
    assert server.article_body(feed, i, root, 1) != server.article_body(feed, i, root, 2)
