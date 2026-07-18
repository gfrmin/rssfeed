"""Property/invariant regression corpus for the extraction pipeline.

Each fixture is a real page frozen under ``tests/fixtures/extraction/_raw/``
(gitignored — see the manifest). Absent blobs *skip*, so this stays green on a
fresh clone / in CI. Populate the corpus with the bootstrap fetcher, then run it:

    uv run python tests/fixtures/extraction/fetch_fixtures.py
    uv run pytest -m corpus
"""
import sys
from pathlib import Path

import pytest

from app.extractor import _extract

# The manifest lives beside its (gitignored) blobs; add that dir to the path so it
# imports without needing the fixtures tree to be a package.
sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "extraction"))
from manifest import FIXTURES, assert_fixture  # noqa: E402


@pytest.mark.corpus
@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.slug)
def test_extraction_corpus(fx):
    blob = fx.raw_path()
    if not blob.exists():
        pytest.skip(
            f"corpus blob absent for {fx.slug} — run "
            "tests/fixtures/extraction/fetch_fixtures.py"
        )
    html = blob.read_text(encoding="utf-8", errors="replace")
    result = _extract(html, fx.url, fx.extract_rules, proxy_images=False)
    assert_fixture(fx, result)
