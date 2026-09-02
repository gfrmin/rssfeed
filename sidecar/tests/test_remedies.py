"""The remedy registry: what each failure cause is, and what can be done about it.

The registry is the contract between a diagnosis (`feed_health.error_bucket`)
and an action (a route that already exists). These tests hold that contract
together from both ends: every bucket the classifier can produce must be
answerable, and every answer must point at a route the app actually serves.
"""
import pytest

from app.feed_health import BUCKET_LABELS
from app.main import app
from app.remedies import CAUSES, Remedy, group_by_cause, remedies_for

ALL_BUCKETS = set(BUCKET_LABELS) - {"ok"}


def _routes() -> set[tuple[str, str]]:
    """(method, path) pairs the app actually serves."""
    return {
        (method, r.path)
        for r in app.routes
        for method in getattr(r, "methods", ()) or ()
    }


def _every_remedy():
    for bucket, cause in CAUSES.items():
        for r in cause.remedies:
            yield bucket, r


# ---------------------------------------------------------------- coverage

def test_every_failure_bucket_has_a_cause():
    assert ALL_BUCKETS <= set(CAUSES), ALL_BUCKETS - set(CAUSES)


def test_no_cause_invents_a_bucket_the_classifier_cannot_produce():
    assert set(CAUSES) <= set(BUCKET_LABELS), set(CAUSES) - set(BUCKET_LABELS)


def test_every_cause_offers_at_least_one_remedy():
    empty = [b for b, c in CAUSES.items() if not c.remedies]
    assert not empty, empty


def test_every_cause_offers_at_least_one_thing_you_can_actually_do():
    """A group of feeds with nothing but roadmap items is a dead end."""
    dead = [b for b, c in CAUSES.items() if not any(r.available for r in c.remedies)]
    assert not dead, dead


def test_remedies_for_unknown_bucket_is_empty_not_an_error():
    assert remedies_for("no-such-bucket") == ()


# ---------------------------------------------------------------- honesty

def test_available_remedies_point_at_a_route_the_app_serves():
    routes = _routes()
    missing = [
        (b, r.id, r.method, r.endpoint)
        for b, r in _every_remedy()
        if r.available and (r.method, r.endpoint) not in routes
    ]
    assert not missing, missing


def test_unbuilt_remedies_carry_no_endpoint_and_say_what_is_missing():
    for bucket, r in _every_remedy():
        if not r.available:
            assert r.endpoint is None, (bucket, r.id)
            assert r.note, f"{bucket}/{r.id} is unbuilt but does not say what it needs"


def test_availability_is_derived_from_the_endpoint_not_asserted_separately():
    assert Remedy(id="x", name="n", desc="d", cta="c").available is False
    assert Remedy(id="x", name="n", desc="d", cta="c",
                  endpoint="/feeds/bulk-refresh").available is True


def test_bulk_remedies_take_a_feed_id_list_and_feed_remedies_take_a_path():
    for bucket, r in _every_remedy():
        if not r.available:
            continue
        if r.scope == "bulk":
            assert "{feed_id}" not in r.endpoint, (bucket, r.id)
        else:
            assert "{feed_id}" in r.endpoint, (bucket, r.id)


def test_remedy_ids_are_unique_within_a_cause():
    for bucket, cause in CAUSES.items():
        ids = [r.id for r in cause.remedies]
        assert len(ids) == len(set(ids)), bucket


def test_one_id_always_means_one_remedy_across_causes():
    """Shared remedies are shared objects, not copies that can drift apart."""
    seen: dict[str, Remedy] = {}
    for bucket, r in _every_remedy():
        assert seen.setdefault(r.id, r) == r, (bucket, r.id)


def test_no_remedy_deletes_a_feed():
    """Feed records are attachment points for a historical import. Pause, never delete."""
    for bucket, r in _every_remedy():
        assert "delete" not in (r.endpoint or ""), (bucket, r.id)
        assert "delete" not in r.name.lower(), (bucket, r.id)


def test_pausing_is_offered_wherever_a_feed_may_be_beyond_repair():
    for bucket in ("cloudflare", "forbidden", "http_404", "not_a_feed"):
        assert any(r.id == "pause" for r in CAUSES[bucket].remedies), bucket


# ------------------------------------------------- the split earns its keep

def test_cloudflare_and_forbidden_do_not_get_the_same_advice():
    cf = {r.id for r in remedies_for("cloudflare")}
    fb = {r.id for r in remedies_for("forbidden")}
    assert cf != fb


def test_a_forbidden_feed_is_not_told_to_solve_a_javascript_challenge():
    assert "unblocker" not in {r.id for r in remedies_for("forbidden")}


def test_paused_feeds_are_offered_resumption():
    assert "resume" in {r.id for r in remedies_for("paused")}


def test_quiet_feeds_are_not_offered_a_fetch_fix():
    """Nothing is failing to fetch — suggesting a proxy would be noise."""
    ids = {r.id for r in remedies_for("quiet")}
    assert not ids & {"proxy-on", "unblocker", "allow-self-signed"}


# ---------------------------------------------------------------- grouping

def _feed(fid, health, bucket, title="Feed"):
    return {"id": fid, "title": title, "_health": health, "_bucket": bucket}


def test_group_by_cause_gathers_feeds_sharing_a_bucket():
    groups = group_by_cause([
        _feed(1, "error", "cloudflare"),
        _feed(2, "error", "cloudflare"),
        _feed(3, "error", "forbidden"),
    ])
    assert {g.bucket: len(g.feeds) for g in groups} == {"cloudflare": 2, "forbidden": 1}


def test_group_by_cause_leaves_healthy_feeds_out():
    assert group_by_cause([_feed(1, "ok", "ok"), _feed(2, "ok", "ok")]) == []


def test_group_by_cause_orders_worst_state_first():
    groups = group_by_cause([
        _feed(1, "paused", "paused"),
        _feed(2, "quiet", "quiet"),
        _feed(3, "warn", "server_5xx"),
        _feed(4, "error", "forbidden"),
        _feed(5, "stale", "stale"),
    ])
    assert [g.bucket for g in groups] == [
        "forbidden", "server_5xx", "stale", "quiet", "paused"]


def test_group_by_cause_puts_the_bigger_group_first_within_a_state():
    groups = group_by_cause([
        _feed(1, "error", "forbidden"),
        _feed(2, "error", "cloudflare"),
        _feed(3, "error", "cloudflare"),
    ])
    assert [g.bucket for g in groups] == ["cloudflare", "forbidden"]


def test_group_by_cause_carries_the_cause_label_and_remedies():
    (g,) = group_by_cause([_feed(1, "error", "cloudflare")])
    assert g.label == CAUSES["cloudflare"].label
    assert g.remedies == CAUSES["cloudflare"].remedies
    assert g.count == 1


def test_group_by_cause_keeps_feed_order_within_a_group():
    groups = group_by_cause([
        _feed(1, "error", "forbidden", "B"),
        _feed(2, "error", "forbidden", "A"),
    ])
    assert [f["id"] for f in groups[0].feeds] == [1, 2]


def test_group_by_cause_tolerates_a_bucket_with_no_registry_entry():
    """A new Miniflux error string must not blank the page."""
    (g,) = group_by_cause([_feed(1, "error", "brand_new_bucket")])
    assert g.bucket == "brand_new_bucket"
    assert g.remedies == ()
    assert g.label


@pytest.mark.parametrize("bucket", sorted(ALL_BUCKETS))
def test_every_bucket_survives_grouping(bucket):
    (g,) = group_by_cause([_feed(1, "error", bucket)])
    assert g.label and g.why
