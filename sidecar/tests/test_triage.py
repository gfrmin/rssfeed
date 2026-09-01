"""The triage view: broken feeds grouped by cause, with the fix attached.

Driven through TestClient like the other endpoint tests — no Postgres, no
Miniflux, no network. The fake connection answers the cadence query with real
rows, because publisher silence is one of the states the page has to show and
an empty result would quietly hide it.
"""
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import cadence, credvault, embeddings, miniflux_client, ranker_client
from app.main import app
from app.routes import cookies as cookies_routes
from app.routes import entries as entries_routes
from app.routes import feeds as feeds_routes

NOW = datetime.now(UTC)

CLOUDFLARE = ("This website is protected by a Cloudflare bot challenge (CAPTCHA "
              "or JavaScript verification). Miniflux cannot solve this challenge "
              "automatically.")
FORBIDDEN = ("Access to this website is forbidden. Perhaps, this website has a "
             "bot protection mechanism?")
AUTH = ("Access to this website is not authorized. It could be a bad username "
        "or password.")


def _feed(fid, title, *, msg="", count=0, disabled=False):
    return {
        "id": fid, "title": title, "site_url": f"https://{fid}.example/",
        "feed_url": f"https://{fid}.example/rss",
        "checked_at": (NOW - timedelta(minutes=10)).isoformat(),
        "parsing_error_count": count, "parsing_error_message": msg,
        "disabled": disabled, "category": {"id": 1, "title": "News"},
    }


FEEDS = [
    _feed(1, "Challenged One", msg=CLOUDFLARE, count=9),
    _feed(2, "Challenged Two", msg=CLOUDFLARE, count=9),
    _feed(3, "Refused One", msg=FORBIDDEN, count=40),
    _feed(4, "Refused Two", msg=FORBIDDEN, count=3),
    _feed(5, "Locked Out", msg=AUTH, count=5),
    _feed(6, "Gone Quiet Recently"),
    _feed(7, "Gone Quiet Long Ago"),
    _feed(8, "Perfectly Fine"),
    _feed(9, "Resting", disabled=True),
]

# feed_id -> (days since newest entry, median gap in hours)
CADENCE = {
    1: (1, 6), 2: (1, 6), 3: (1, 6), 4: (1, 6), 5: (1, 6),
    6: (3, 1),        # hourly publisher, silent three days -> quiet
    7: (800, 24),     # daily publisher, silent since forever -> quiet
    8: (0, 24),       # daily publisher, posted today -> fine
    9: (400, 24),
}


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    async def execute(self, sql, params=None):
        if "percentile_cont" in sql:
            return _Cursor([
                {"feed_id": fid,
                 "latest": NOW - timedelta(days=days),
                 "median_gap_s": float(gap_h * 3600)}
                for fid, (days, gap_h) in CADENCE.items()
            ])
        if "count(*)" in sql.lower() and "group by" not in sql.lower():
            return _Cursor([{"n": 0, "cnt": 0}])
        return _Cursor([])

    async def commit(self):
        pass


@contextlib.asynccontextmanager
async def _fake_get_conn():
    yield _Conn()


@pytest.fixture
def client(monkeypatch):
    async def get_feeds():
        return [dict(f) for f in FEEDS]

    async def get_feed_counters():
        return {"unreads": {}, "reads": {}}

    async def get_entries(**kw):
        return {"total": 0, "entries": []}

    async def get_categories():
        return [{"id": 1, "title": "News"}]

    for name, fn in [("get_feeds", get_feeds), ("get_feed_counters", get_feed_counters),
                     ("get_entries", get_entries), ("get_categories", get_categories)]:
        monkeypatch.setattr(miniflux_client, name, fn)
    for mod in (entries_routes, feeds_routes, cookies_routes, cadence):
        monkeypatch.setattr(mod, "get_conn", _fake_get_conn)

    async def score(articles):
        return None

    async def embed_sims(conn, entry_ids):
        return {}

    async def has_credentials(domain):
        return False

    monkeypatch.setattr(ranker_client, "score", score)
    monkeypatch.setattr(embeddings, "embed_sims", embed_sims)
    monkeypatch.setattr(credvault, "has_credentials", has_credentials)
    entries_routes._invalidate_sidebar_cache()
    cadence.invalidate()
    return TestClient(app)


@pytest.fixture
def all_healthy(monkeypatch, client):
    async def get_feeds():
        return [dict(_feed(8, "Perfectly Fine"))]

    monkeypatch.setattr(miniflux_client, "get_feeds", get_feeds)
    entries_routes._invalidate_sidebar_cache()
    cadence.invalidate()
    return client


def _pane(client, bucket: str) -> str:
    """Just the cause pane — the shell around it carries the whole feed list."""
    r = client.get(f"/triage/{bucket}",
                   headers={"HX-Request": "true", "HX-Target": "reader-col"})
    assert r.status_code == 200
    return r.text


# ------------------------------------------------------------------ the page

def test_triage_page_loads(client):
    assert client.get("/triage").status_code == 200


def test_triage_groups_feeds_by_cause_not_by_feed(client):
    body = client.get("/triage").text
    assert "Cloudflare challenge" in body
    assert "Access forbidden" in body
    assert "Credentials rejected" in body


def test_triage_leads_with_the_worst_and_largest_group(client):
    body = client.get("/triage").text
    # two Cloudflare, two forbidden, one auth — errors first, biggest first,
    # and the quiet publishers below all of them.
    assert body.index("Cloudflare challenge") < body.index("Credentials rejected")
    assert body.index("Credentials rejected") < body.index("Publisher has gone quiet")


def test_triage_counts_each_group(client):
    body = client.get("/triage").text
    assert ">2<" in body  # both error groups have two feeds


def test_triage_leaves_healthy_feeds_out(client):
    """A healthy feed forms no group. (The sidebar still lists it — that is its job,
    which is why the assertions below read the cause pane, not the whole page.)"""
    assert "Unclassified" not in client.get("/triage").text
    assert "Perfectly Fine" not in _pane(client, "cloudflare")


def test_triage_says_so_when_there_is_nothing_to_answer(all_healthy):
    body = all_healthy.get("/triage").text
    assert "Nothing needs attention" in body


# ------------------------------------------------------------ a focused cause

def test_a_cause_explains_itself(client):
    body = client.get("/triage/cloudflare").text
    assert "challenge page instead of a feed" in body


def test_a_cause_lists_the_feeds_it_covers(client):
    pane = _pane(client, "cloudflare")
    assert "Challenged One" in pane and "Challenged Two" in pane
    assert "Refused One" not in pane


def test_a_cause_shows_the_raw_error_it_matched(client):
    assert "Cloudflare bot challenge" in client.get("/triage/cloudflare").text


def test_a_bulk_remedy_posts_the_whole_group_at_once(client):
    body = client.get("/triage/cloudflare").text
    assert 'hx-post="/feeds/auto-discover"' in body
    assert 'name="feed_ids" value="1"' in body
    assert 'name="feed_ids" value="2"' in body


def test_a_remedy_that_is_not_built_is_explained_not_offered(client):
    body = client.get("/triage/cloudflare").text
    assert "needs a per-feed fetch route setting" in body
    # the unbuilt remedy must not arrive as a button that does nothing
    assert "Route via unblocker</button>" not in body


def test_a_per_feed_remedy_points_at_each_feed_rather_than_the_group(client):
    body = client.get("/triage/auth").text
    assert 'href="/feeds/5"' in body


def test_the_quiet_group_puts_this_weeks_silence_above_the_ancient(client):
    pane = _pane(client, "quiet")
    assert pane.index("Gone Quiet Recently") < pane.index("Gone Quiet Long Ago")


def test_the_quiet_group_shows_how_old_its_silences_are(client):
    body = client.get("/triage/quiet").text
    assert "past week" in body and "over a year" in body


def test_an_unknown_cause_is_a_404(client):
    assert client.get("/triage/not-a-cause").status_code == 404


def test_a_cause_with_no_feeds_right_now_is_a_404(client):
    """`tls` is a real bucket, but nothing is in it — do not render an empty pane."""
    assert client.get("/triage/tls").status_code == 404


def test_a_focused_cause_arrives_as_a_fragment_for_htmx(client):
    r = client.get("/triage/cloudflare",
                   headers={"HX-Request": "true", "HX-Target": "reader-col"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()


def test_a_focused_cause_is_a_whole_page_on_a_cold_load(client):
    r = client.get("/triage/cloudflare")
    assert "<html" in r.text.lower()


# ---------------------------------------------------------------- wayfinding

def test_the_sidebar_offers_a_way_into_triage(client):
    assert 'href="/triage"' in client.get("/entries").text


def test_the_sidebar_counts_what_needs_attention(client):
    """Eight of the nine feeds are unwell; the ninth is fine."""
    body = client.get("/entries").text
    assert "Needs attention" in body


def test_the_sidebar_says_nothing_when_all_is_well(all_healthy):
    body = all_healthy.get("/entries").text
    assert "Needs attention" not in body


# ------------------------------------------- what the numbers actually claim

def test_the_header_counts_problems_not_deliberate_pauses(client):
    """Seven feeds are unwell and one is paused; the headline is seven."""
    body = client.get("/triage").text
    assert 'class="list-count">7<' in body


def test_a_paused_group_is_still_listed_even_though_it_is_not_counted(client):
    assert "Polling paused" in client.get("/triage").text


def test_a_broken_feed_is_not_described_as_silent(client):
    """It has a fetch error. How long since it last published is not the story,
    and `silent 8d` on a 403 reads as a diagnosis it is not."""
    pane = _pane(client, "cloudflare")
    assert "silent" not in pane
    assert "failed polls" in pane


def test_a_quiet_feed_says_how_long_it_has_been_silent(client):
    pane = _pane(client, "quiet")
    assert "silent 3d" in pane      # went quiet three days ago
    assert "silent 2y" in pane      # and one stopped 800 days ago
