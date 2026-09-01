"""Every asset a template references must exist on disk.

`base.html` has linked `/static/tailwind.css` since the design-system port, and
that file was never built or committed -- so every page rendered through it (feed
management, cookies, the diff overlay) came up with its form controls, tables and
buttons unstyled for anyone who cloned the repo. The reader shell hides the
problem, because its styling is hand-written in style.css.

Nothing failed: a missing stylesheet is a 404 in the browser console, and the
suite never opens a browser. So the check is on the reference itself.
"""
import re
from pathlib import Path

import pytest

SIDECAR = Path(__file__).resolve().parents[1]
TEMPLATES = SIDECAR / "app" / "templates"
STATIC = SIDECAR / "static"

# href="/static/x.css?v=1" / src="/static/x.js" -- the query string is a cache
# buster, not part of the filename.
_REF = re.compile(r'(?:href|src)="/static/([^"?]+)')


def _template_refs():
    for tpl in sorted(TEMPLATES.rglob("*.html")):
        for name in _REF.findall(tpl.read_text()):
            yield tpl.name, name


@pytest.mark.parametrize("tpl,asset", sorted(set(_template_refs())))
def test_referenced_static_asset_exists(tpl, asset):
    path = STATIC / asset
    assert path.is_file(), f"{tpl} references /static/{asset}, which does not exist"
    assert path.stat().st_size > 0, f"/static/{asset} is empty"


def test_built_css_is_present_and_looks_built():
    """The Tailwind output is a build artifact, and it is committed on purpose.

    Committing it is what lets `git clone && uv sync && ./run-sidecar.sh` produce a
    styled app with no Node toolchain. If it is ever regenerated empty -- a failed
    build writing a 0-byte file is the usual way -- this fails rather than shipping
    a blank stylesheet.
    """
    css = (STATIC / "tailwind.css").read_text()
    assert len(css) > 2000, "tailwind.css is suspiciously small; did the build fail?"
    assert "--tw-" in css or "tailwindcss" in css, "tailwind.css does not look like Tailwind output"


# Tailwind display utilities. style.css must leave these alone -- see the test.
# `.grid` is deliberately NOT in this set: the shell owns it for the three-pane
# layout, and no page that loads Tailwind's grid also loads the shell's.
_TAILWIND_DISPLAY_UTILITIES = frozenset({
    "hidden", "block", "inline", "inline-block", "flex", "inline-flex",
    "table", "table-cell", "table-row", "contents",
})


def test_the_shell_stylesheet_does_not_redefine_a_tailwind_display_utility():
    """base.html loads style.css *after* tailwind.css, so a display rule here
    beats every Tailwind display utility — responsive variants included, because
    a media query adds no specificity.

    `.hidden { display:none !important }` did exactly that. The feeds table's
    Category and Latest columns are `hidden sm:table-cell`; they were hidden at
    every width, on every screen, for as long as both sheets have been loaded.
    Nothing failed, because a column that never appears looks like a decision.
    """
    style = (STATIC / "style.css").read_text()
    defined = set(re.findall(r"^\.([a-z][a-z0-9-]*)\s*\{", style, re.M))
    clashes = defined & _TAILWIND_DISPLAY_UTILITIES
    assert not clashes, sorted(clashes)
