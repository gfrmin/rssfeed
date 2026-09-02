"""The command palette.

The palette itself is browser JavaScript, so what is testable here is its
seams: the routes it claims to reach must exist, and the pages it reads its
feed list out of must actually mark the feeds up for it. Both are the kind of
thing that breaks silently — a renamed route leaves a dead menu entry, and a
dropped attribute leaves a palette that finds nothing and looks broken.
"""
import re
from pathlib import Path

from app.main import app

SIDECAR = Path(__file__).resolve().parents[1]
PALETTE = SIDECAR / "static" / "palette.js"
TEMPLATES = SIDECAR / "app" / "templates"


def _get_routes() -> set[str]:
    return {r.path for r in app.routes if "GET" in (getattr(r, "methods", ()) or ())}


def _palette_hrefs() -> list[str]:
    return re.findall(r"href: '(/[^']*)'", PALETTE.read_text())


def test_the_palette_ships():
    assert PALETTE.is_file()


def test_base_html_loads_the_palette_on_every_page():
    """Including the management pages, which have no reader shell to hang it off."""
    assert "palette.js" in (TEMPLATES / "base.html").read_text()


def test_every_place_the_palette_offers_to_go_is_a_real_route():
    routes = _get_routes()
    hrefs = _palette_hrefs()
    assert hrefs, "no navigation entries found — the regex or the file changed shape"
    missing = [h for h in hrefs if h.split("?")[0] not in routes]
    assert not missing, missing


def test_the_palette_reaches_the_views_the_sidebar_reaches():
    """Anything on the sidebar should be typeable. A palette that knows fewer
    places than the nav rail is a palette people stop opening."""
    offered = " ".join(_palette_hrefs())
    for view in ("unread", "all", "read", "starred", "changed"):
        assert f"view={view}" in offered, view
    assert "/triage" in offered
    assert "/cookies" in offered


def test_the_sidebar_marks_its_feeds_for_the_palette():
    assert "data-palette-feed" in (TEMPLATES / "_sidebar.html").read_text()


def test_the_feeds_page_marks_its_feeds_for_the_palette():
    """The management pages have no sidebar, so this is the palette's only
    source of feeds there."""
    assert "data-palette-feed" in (TEMPLATES / "feeds.html").read_text()


def test_the_palette_can_open_a_feeds_settings():
    """The wayfinding gap: a broken feed's settings had no route from the reader
    that did not go through an article the feed does not have."""
    js = PALETTE.read_text()
    assert "/feeds/' +" in js or "/feeds/${" in js


def test_the_palette_marks_causes_where_causes_are_rendered(client):
    body = client.get("/").text
    assert "data-palette-cause" in body


def test_the_palette_can_be_opened_without_a_keyboard():
    """There is no ⌘K on a phone, and a palette nobody can find is a palette
    nobody uses. Every page chrome carries a visible trigger."""
    assert "data-palette-open" in (TEMPLATES / "_sidebar.html").read_text()
    assert "data-palette-open" in (TEMPLATES / "base.html").read_text()
    assert "data-palette-open" in PALETTE.read_text()


def test_the_script_can_relabel_the_shortcut_hint():
    """The hint is printed as ⌘K, which is a key this machine does not have.

    The binding already accepts Ctrl, so the shortcut works — it is the
    *label* that lies, and a wrong label is worse than none: it tells the
    only user of this reader to press something their keyboard has not got.
    The script relabels it at load, so it must know both the hook and the
    word it substitutes.
    """
    js = PALETTE.read_text()
    assert "data-palette-key" in js, "the script never relabels the visible hint"
    assert "Ctrl" in js, "the script has nothing to relabel the hint to"


def test_no_template_prints_a_command_glyph_the_script_cannot_reach():
    """Every visible ⌘ has to sit on an element the relabel can find.

    A new trigger that spells the hint out by hand looks right on a Mac and
    is wrong everywhere else, and nothing else in the suite would notice.
    """
    for tpl in sorted(TEMPLATES.glob("*.html")):
        for n, line in enumerate(tpl.read_text().splitlines(), 1):
            if "⌘" not in line and "&#8984;" not in line:
                continue
            assert "data-palette-key" in line or "data-palette-open" in line, (
                f"{tpl.name}:{n} prints ⌘ where the palette script cannot relabel it:"
                f" {line.strip()}"
            )
