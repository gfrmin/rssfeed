"""Load content.py into the seed Miniflux, then shape it through the sidecar.

    python seed/seed.py miniflux --env-out FILE   # subscribe, break, pause; writes the API key env
    python seed/seed.py sidecar                    # priorities, full text, reads, stars, ratings

Phase 1 talks to Miniflux only. Phase 2 goes through the sidecar's own routes
(not its tables), so the seed exercises the code a reader would. Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from content import CHANGING, FEEDS  # noqa: E402

MINIFLUX = os.environ.get("SEED_MINIFLUX_URL", "http://127.0.0.1:9146")
SIDECAR = os.environ.get("SEED_SIDECAR_URL", "http://127.0.0.1:9147")
SERVER = os.environ.get("SEED_SERVER_URL", "http://127.0.0.1:9148")
BASIC = "Basic " + base64.b64encode(b"seed:seed-only-password").decode()


def call(method: str, url: str, body=None, form=None, headers=None, expect_fail=False):
    data, hdrs = None, dict(headers or {})
    if body is not None:
        data, hdrs["Content-Type"] = json.dumps(body).encode(), "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if expect_fail:
            return None
        raise SystemExit(f"{method} {url} -> {e.code}: {e.read()[:300]!r}") from e
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw.decode(errors="replace")


def mf(method: str, path: str, body=None, expect_fail=False):
    return call(method, MINIFLUX + "/v1" + path, body, headers={"Authorization": BASIC},
                expect_fail=expect_fail)


def feed_ids() -> dict[str, int]:
    """slug -> Miniflux feed id. Matched on title: two feeds' URLs are moved off the server."""
    by_title = {f["title"]: f["id"] for f in mf("GET", "/feeds")}
    return {f.slug: by_title[f.title] for f in FEEDS if f.title in by_title}


def entries(feed_id: int) -> list[dict]:
    """Newest first, matching content.py's index order."""
    return mf("GET", f"/feeds/{feed_id}/entries?limit=200&order=published_at&direction=desc")["entries"]


def phase_miniflux(env_out: Path) -> None:
    cats = {c["title"]: c["id"] for c in mf("GET", "/categories")}
    for name in sorted({f.category for f in FEEDS} - cats.keys()):
        cats[name] = mf("POST", "/categories", {"title": name})["id"]
    have = {f["feed_url"] for f in mf("GET", "/feeds")}
    for f in FEEDS:
        url = f"{SERVER}/feeds/{f.slug}.xml"
        if url not in have:
            mf("POST", "/feeds", {"feed_url": url, "category_id": cats[f.category]})
    ids = feed_ids()

    call("POST", SERVER + "/_break")
    moved = {"refused": "http://127.0.0.1:9/feed.xml", "nowhere": "http://nowhere.invalid/feed.xml"}
    for slug, url in moved.items():
        mf("PUT", f"/feeds/{ids[slug]}", {"feed_url": url})
    for f in FEEDS:
        if f.paused:
            mf("PUT", f"/feeds/{ids[f.slug]}", {"disabled": True})
    failing = [ids[f.slug] for f in FEEDS if f.fail]
    for _ in range(3):  # feed_health calls a failure persistent at 3
        for fid in failing:
            mf("PUT", f"/feeds/{fid}/refresh", expect_fail=True)  # failing is the point
        time.sleep(1)

    key = mf("POST", "/api-keys", {"description": f"seed {int(time.time())}"})["token"]
    env_out.write_text(f"MINIFLUX_API_KEY={key}\n")
    print(f"miniflux seeded: {len(ids)} feeds, {len(failing)} failing; key -> {env_out}")


def side(path: str, form=None, body=None):
    return call("POST", SIDECAR + path, body=body, form=form if form is not None else ({} if body is None else None))


def phase_sidecar() -> None:
    ids = feed_ids()
    for f in FEEDS:
        if f.priority != 2:
            side(f"/feeds/{ids[f.slug]}/set-priority", {"priority": f.priority})
        if f.full_content:
            side(f"/feeds/{ids[f.slug]}/toggle-full-content")
    side(f"/feeds/{ids['signal']}/mute", {"kind": "author", "value": "Digest bot (sponsored)"})
    side(f"/feeds/{ids['marginalia']}/mute", {"kind": "tag", "value": "rain"})

    by_feed = {slug: entries(ids[slug]) for slug in ("tidewater", "signal", "marginalia", "kettle")}
    read = ([e["id"] for e in by_feed["tidewater"][8:]] + [e["id"] for e in by_feed["signal"][5:]]
            + [e["id"] for e in by_feed["marginalia"][3:]])
    mf("PUT", "/entries", {"entry_ids": read, "status": "read"})
    for e in (by_feed["marginalia"][0], by_feed["tidewater"][2]):
        side(f"/entries/{e['id']}/toggle-star")
    for e, d in ((by_feed["tidewater"][0], "up"), (by_feed["kettle"][1], "up"), (by_feed["signal"][0], "down")):
        side(f"/entries/{e['id']}/thumb", body={"dir": d, "feed_id": e["feed_id"]})
    for e in by_feed["tidewater"][:4]:
        side(f"/entries/{e['id']}/event", body={"signal": "dwell", "value": 30, "feed_id": e["feed_id"]})

    slug, i = CHANGING
    changing = entries(ids[slug])[i]["id"]
    for _ in range(2):  # first fetch is v1, the server changes the page, second is v2
        side(f"/entries/{changing}/fetch-full")
    print(f"sidecar seeded: {len(read)} read, changing entry {changing}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("miniflux", "sidecar"))
    ap.add_argument("--env-out", type=Path)
    a = ap.parse_args()
    if a.phase == "miniflux":
        if not a.env_out:
            ap.error("--env-out is required for the miniflux phase")
        phase_miniflux(a.env_out)
    else:
        phase_sidecar()


if __name__ == "__main__":
    main()
