"""Ground-truth corpus for article extraction — property/invariant assertions.

**Public repo: this file holds only URLs + *structural* assertions, never article
prose.** The raw article HTML lives in gitignored ``_raw/<slug>.html`` (populate
with ``fetch_fixtures.py``); the regression suite (``test_extraction_corpus.py``)
skips any fixture whose blob is absent, so a fresh clone / CI stays green.

Assertions are deliberately structural — anchor counts, href hosts, media counts,
a max text length (a boilerplate/widget leak shows up as excess text), a coverage
floor (the rendered body must retain most of the extracted prose — catches
truncation), and forbidden boilerplate ids. That keeps the corpus faithful to real
DOM pathology without ever checking real content into git.
"""
from dataclasses import dataclass, field
from pathlib import Path

RAW_DIR = Path(__file__).parent / "_raw"

_MEDIA_XPATH = "//img[@src] | //video[@src] | //audio[@src] | //iframe[@src] | //source[@src]"


@dataclass(frozen=True)
class Fixture:
    slug: str            # -> _raw/<slug>.html
    url: str             # for the bootstrap fetcher and as the extraction base URL
    kind: str            # failure-mode tag, for humans
    extract_rules: dict = field(default_factory=dict)
    # --- property assertions (None / empty tuple = "don't check") ---
    min_anchors: int | None = None            # >= this many surviving a[href]
    expect_href_hosts: tuple[str, ...] = ()   # each host must appear in some href
    min_media: int | None = None              # >= this many src-bearing media nodes
    max_text_len: int | None = None           # <= this — excess flags a boilerplate leak
    min_coverage: float | None = None          # rendered body text >= this * content_text
    forbid_boiler_ids: tuple[str, ...] = ()   # e.g. "div-gpt-ad" must not appear
    expect_none: bool = False                  # extraction must reject (paywall shell)

    def raw_path(self) -> Path:
        return RAW_DIR / f"{self.slug}.html"


# ~Guido-heavy, spanning the failure modes we verified against the live site, plus
# two feeds that showed wholesale link loss (thejc = feed 150, spiked = feed 413).
FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        slug="guido-blip-archive-link",
        url="https://order-order.com/2026/07/17/hope-not-hate-deletes-page-attacking-ann-widdecombe/",
        kind="short-blip-with-links",
        min_anchors=2,
        expect_href_hosts=("web.archive.org",),
        max_text_len=300,               # real body ~100 chars; a QOTD-widget leak is ~950
        min_coverage=0.75,
    ),
    Fixture(
        slug="guido-live-pmqs",
        url="https://order-order.com/2026/07/15/live-pmqs-48/",
        kind="video-embed",
        min_media=1,                    # the YouTube livestream is the whole post
        max_text_len=60,                # essentially text-free; excess = widget leak
    ),
    Fixture(
        slug="guido-watch-dancing",
        url="https://order-order.com/2026/07/17/watch-andy-burnham-starts-dancing-after-taking-over-labour-party/",
        kind="video-embed-ad-trap",
        min_media=1,
        max_text_len=60,
        forbid_boiler_ids=("div-gpt-ad", "fb-pxl-ajax-code"),
    ),
    Fixture(
        slug="guido-live-farage",
        url="https://order-order.com/2026/07/17/live-farage-speaks-at-cpac-gb/",
        kind="video-embed",
        min_media=1,
        max_text_len=60,
    ),
    Fixture(
        slug="thejc-linky",
        url="https://www.thejc.com/news/world/europes-smallest-jewish-community-synagogue-n0piexf8",
        kind="wholesale-link-loss-feed",
        min_anchors=2,                  # feed 150 sat at 2.3% anchor presence pre-fix
        min_coverage=0.75,
    ),
    Fixture(
        slug="spiked-linky",
        url="https://www.spiked-online.com/2026/07/17/why-television-isnt-funny-anymore/",
        kind="wholesale-link-loss-feed",
        min_anchors=4,                  # feed 413 sat at 10%
        min_coverage=0.75,
    ),
)


def assert_fixture(fx: Fixture, result: dict | None) -> None:
    """Check a fixture's structural invariants against an extraction result."""
    import lxml.html as lxml_html

    if fx.expect_none:
        assert result is None, f"{fx.slug}: expected rejection, got a snapshot"
        return

    assert result is not None, f"{fx.slug}: extraction returned None"
    html = result["content_html"]
    tree = lxml_html.fromstring(f"<div>{html}</div>")
    anchors = tree.xpath("//a[@href]")

    if fx.min_anchors is not None:
        assert len(anchors) >= fx.min_anchors, \
            f"{fx.slug}: {len(anchors)} anchors < required {fx.min_anchors}"
    for host in fx.expect_href_hosts:
        assert any(host in (a.get("href") or "") for a in anchors), \
            f"{fx.slug}: no href pointing at {host}"
    if fx.min_media is not None:
        media = tree.xpath(_MEDIA_XPATH)
        assert len(media) >= fx.min_media, \
            f"{fx.slug}: {len(media)} media < required {fx.min_media}"
    if fx.max_text_len is not None:
        n = len(result["content_text"])
        assert n <= fx.max_text_len, \
            f"{fx.slug}: content_text {n} chars > {fx.max_text_len} (boilerplate leak?)"
    for bid in fx.forbid_boiler_ids:
        assert bid not in html, f"{fx.slug}: boilerplate marker {bid!r} present in body"
    if fx.min_coverage is not None:
        content_len = len(result["content_text"])
        body_len = len(" ".join(tree.text_content().split()))
        assert content_len and body_len >= fx.min_coverage * content_len, \
            f"{fx.slug}: rendered body {body_len} < {fx.min_coverage:.0%} of " \
            f"content_text {content_len} (truncation?)"
