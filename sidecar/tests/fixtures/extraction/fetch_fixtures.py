"""Bootstrap the gitignored extraction corpus: download each fixture's raw HTML.

The raw pages are real feed content and are never committed (see the repo's
public-repo rule); this repopulates them locally from the committed manifest.

    cd sidecar
    uv run python tests/fixtures/extraction/fetch_fixtures.py          # fetch missing
    uv run python tests/fixtures/extraction/fetch_fixtures.py --force  # refetch all

Then run the corpus:  uv run pytest -m corpus
"""
import sys

import httpx

# Run as a script: this file's directory is on sys.path, so `manifest` imports.
from manifest import FIXTURES, RAW_DIR

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def main() -> int:
    force = "--force" in sys.argv
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for fx in FIXTURES:
        dest = fx.raw_path()
        if dest.exists() and not force:
            print(f"skip (exists): {fx.slug}")
            continue
        try:
            r = httpx.get(fx.url, headers={"User-Agent": _UA},
                          follow_redirects=True, timeout=30.0)
            r.raise_for_status()
            dest.write_text(r.text, encoding="utf-8")
            print(f"fetched {fx.slug}  ({len(r.text)} bytes)")
        except Exception as e:  # noqa: BLE001 - a bootstrap script; report and continue
            print(f"FAILED  {fx.slug}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed} fixture(s) failed to fetch.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
