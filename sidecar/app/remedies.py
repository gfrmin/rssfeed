"""What each failure cause is, and what can actually be done about it.

`feed_health` answers "what is wrong with this feed". This module answers the
next question — "so what do I do?" — and it answers it per CAUSE, not per feed.
Twenty-one broken feeds are rarely twenty-one problems: nine of them share one
Cloudflare rule and one fix. Bind the remedy to the cause and the work collapses.

A remedy is only `available` when it names a route the app already serves; a
test walks the app's route table and fails if one drifts. Remedies that are not
built yet carry no endpoint and a `note` saying what they need, so a group can
explain itself honestly instead of offering a button that does nothing.
"""
from dataclasses import dataclass

from app.feed_health import BUCKET_LABELS

# Order matters when a triage list is sorted: worst first.
STATE_ORDER = ("error", "warn", "stale", "quiet", "paused")


@dataclass(frozen=True)
class Remedy:
    """One action offered against a cause.

    `scope` says how it is invoked: "bulk" endpoints take a `feed_ids` list and
    fix a whole group in one call; "feed" endpoints carry `{feed_id}` in the
    path and have to be walked one feed at a time.
    """
    id: str
    name: str
    desc: str
    cta: str
    endpoint: str | None = None
    method: str = "POST"
    scope: str = "bulk"
    note: str = ""          # what it still needs, when there is no endpoint

    @property
    def available(self) -> bool:
        return self.endpoint is not None


@dataclass(frozen=True)
class Cause:
    label: str      # short human name for the bucket
    why: str        # one line: what the classifier saw
    explain: str    # the paragraph that makes the group make sense
    remedies: tuple[Remedy, ...] = ()


# --------------------------------------------------------------------------
# Remedies, defined once and shared. Identity matters: the same fix appearing
# under three causes must be the same object, so it cannot drift into three
# subtly different descriptions of one button.
# --------------------------------------------------------------------------

PROBE_URL = Remedy(
    id="probe-url", cta="Probe", endpoint="/feeds/auto-discover",
    name="Probe for a working feed URL",
    desc="Re-runs discovery against the site and reports any feed URL that "
         "answers. Publishers routinely leave an alternate path — an atom "
         "variant, a syndication mirror, a bare RSS URL — that never picked up "
         "whatever now blocks the old one.",
)

SET_URL = Remedy(
    id="set-url", cta="Set URL", endpoint="/feeds/{feed_id}/set-url", scope="feed",
    name="Point the feed at a new URL",
    desc="Keeps the feed record, its history and its config, and swaps only "
         "where Miniflux polls. The change is recorded so a later historical "
         "import can still match the old address.",
)

REFRESH = Remedy(
    id="refresh", cta="Re-check", endpoint="/feeds/bulk-refresh",
    name="Poll again now",
    desc="Forces a fetch outside the scheduler. Clears the error count if it "
         "succeeds, which is the whole fix whenever the failure was transient.",
)

PAUSE = Remedy(
    id="pause", cta="Pause", endpoint="/feeds/pause-polling",
    name="Pause polling",
    desc="Stops the retries and the noise while keeping every entry and the "
         "feed record itself. Reversible, and the right holding move for a "
         "feed you are not ready to fix or give up on.",
)

RESUME = Remedy(
    id="resume", cta="Resume", endpoint="/feeds/resume-polling",
    name="Resume polling",
    desc="Puts the feed back on the schedule. Worth trying on anything paused "
         "long enough ago that whatever broke it may since have been fixed.",
)

PROXY_ON = Remedy(
    id="proxy-on", cta="Enable", endpoint="/feeds/set-proxy",
    name="Fetch article text through the proxy",
    desc="Routes full-text extraction through the egress proxy. Note this does "
         "not touch the poll itself — if the XML is blocked, entries still stop "
         "arriving; it only rescues the article bodies of entries you already have.",
)

ALLOW_SELF_SIGNED = Remedy(
    id="allow-self-signed", cta="Allow", endpoint="/feeds/allow-self-signed",
    name="Accept this feed's certificate",
    desc="Disables certificate verification for these feeds only. Reasonable "
         "for a hobbyist server with an expired cert; not for anything carrying "
         "a login.",
)

IMPORT_COOKIES = Remedy(
    id="cookies-firefox", cta="Import", scope="feed",
    endpoint="/feeds/{feed_id}/subscription/import-firefox",
    name="Import the session from Firefox",
    desc="Reads the live cookies straight out of the browser profile. The "
         "fastest fix when you are already signed in to the site on this machine.",
)

SUBSCRIPTION_LOGIN = Remedy(
    id="login", cta="Sign in", scope="feed",
    endpoint="/feeds/{feed_id}/subscription/login",
    name="Sign in with stored credentials",
    desc="Drives a real login in a headless browser and keeps the resulting "
         "session in the credential vault, so it can be renewed without you.",
)

PASTE_COOKIES = Remedy(
    id="cookies-paste", cta="Paste", scope="feed",
    endpoint="/feeds/{feed_id}/subscription/paste",
    name="Paste a cookie header",
    desc="Manual fallback for sites whose login flow the browser driver cannot "
         "complete on its own.",
)

# --- not built yet. No endpoint, and each says what it is waiting on. ---

UNBLOCKER = Remedy(
    id="unblocker", cta="Route via unblocker",
    name="Poll these through a browser that can pass the challenge",
    desc="A JavaScript interstitial cannot be solved by an HTTP client, however "
         "it is configured. A headless-browser fetch solves it once and hands "
         "back clean XML. Metered per request, so it wants to be scoped to this "
         "group rather than turned on everywhere.",
    note="needs a per-feed fetch route setting; the proxy credentials already exist",
)

USER_AGENT = Remedy(
    id="user-agent", cta="Set user agent",
    name="Send a browser user agent",
    desc="The most common cause of a flat 403 is the default Go HTTP user "
         "agent. Miniflux already stores a per-feed user agent — the sidecar "
         "has simply never exposed the field.",
    note="needs UI over the existing Miniflux per-feed user_agent field",
)

FLAKY_GRACE = Remedy(
    id="grace-period", cta="Configure",
    name="Ignore a single failed poll",
    desc="One slow fetch should not put a feed on the same shelf as a hard 403. "
         "The threshold that separates warn from error exists; a grace period "
         "before flagging at all does not.",
    note="needs a consecutive-failure floor in feed_health.classify",
)

MUTE_QUIET = Remedy(
    id="mute-quiet", cta="Mute",
    name="Mute this feed's quiet alert",
    desc="Seasonal publications and feeds on a known hiatus should be "
         "silenceable without pausing the poll — pausing would also stop the "
         "entries arriving when they resume.",
    note="needs a per-feed quiet_muted flag in feed_config",
)


# --------------------------------------------------------------------------
# Causes. Every bucket `feed_health.error_bucket` can return, plus the three
# non-error states that still need answering.
# --------------------------------------------------------------------------

CAUSES: dict[str, Cause] = {
    "cloudflare": Cause(
        label="Cloudflare challenge",
        why="a JavaScript interstitial where the XML should be",
        explain="Miniflux receives a challenge page instead of a feed, and its "
                "HTTP client has no way to solve one. Every poll fails "
                "identically, so this is one problem wearing as many names as "
                "there are feeds behind that rule.",
        remedies=(UNBLOCKER, PROBE_URL, PROXY_ON, PAUSE),
    ),
    "forbidden": Cause(
        label="Access forbidden",
        why="a plain 403, with no challenge page",
        explain="The server refuses outright rather than challenging — usually "
                "a user-agent block or an IP reputation rule. That is a "
                "different fix from a Cloudflare interstitial, which is why the "
                "two no longer share a bucket.",
        remedies=(USER_AGENT, PROBE_URL, IMPORT_COOKIES, PAUSE),
    ),
    "bot_blocked": Cause(
        label="Blocked as a bot",
        why="the server named bot protection in its refusal",
        explain="Some kind of automated-traffic filter, not identified further "
                "by the error text. Worth probing for an unprotected URL before "
                "assuming it needs a browser.",
        remedies=(PROBE_URL, UNBLOCKER, PAUSE),
    ),
    "auth": Cause(
        label="Credentials rejected",
        why="the stored login was refused",
        explain="The feed is behind a sign-in the sidecar already knows how to "
                "handle; the session has simply gone stale. This is the one "
                "failure whose fix is built end to end.",
        remedies=(IMPORT_COOKIES, SUBSCRIPTION_LOGIN, PASTE_COOKIES),
    ),
    "http_404": Cause(
        label="Not found",
        why="the feed URL returns 404",
        explain="The address is gone. Almost always the feed moved rather than "
                "ended, and the publisher left no redirect behind.",
        remedies=(PROBE_URL, SET_URL, PAUSE),
    ),
    "not_a_feed": Cause(
        label="Not a feed",
        why="the response parsed as something other than a feed",
        explain="The URL answers, but with a web page — typically a redirect to "
                "a homepage or a paywall notice standing in for the XML.",
        remedies=(PROBE_URL, SET_URL, PAUSE),
    ),
    "server_5xx": Cause(
        label="Server error",
        why="the publisher's server returned a 5xx",
        explain="Their problem, not yours. Most clear up on their own, so the "
                "useful move is to re-check before changing anything.",
        remedies=(REFRESH, PAUSE),
    ),
    "tls": Cause(
        label="Certificate rejected",
        why="the TLS handshake failed",
        explain="Usually an expired or self-signed certificate on a small "
                "server. Accepting it is a per-feed decision, not a global one.",
        remedies=(ALLOW_SELF_SIGNED, REFRESH, PAUSE),
    ),
    "dns_fail": Cause(
        label="Host not found",
        why="the hostname did not resolve",
        explain="The domain itself is unreachable — lapsed, moved, or briefly "
                "misconfigured. Re-check first; a DNS blip and a dead domain "
                "look the same for the first hour.",
        remedies=(REFRESH, PROBE_URL, PAUSE),
    ),
    "connect_fail": Cause(
        label="Could not connect",
        why="no response before the deadline",
        explain="Intermittent rather than broken. A single slow poll should not "
                "have promoted these to the same shelf as a hard refusal.",
        remedies=(REFRESH, FLAKY_GRACE, PAUSE),
    ),
    "unsupported_scheme": Cause(
        label="Unsupported URL",
        why="the feed URL is not something Miniflux can fetch",
        explain="Typically a leftover scheme from an import — feed://, or a "
                "file path that meant something on another machine.",
        remedies=(SET_URL, PAUSE),
    ),
    "other": Cause(
        label="Unrecognised error",
        why="the error text matches no known pattern",
        explain="No pattern matched, so the raw message is the only diagnosis "
                "available. If a group builds up here it is a missing case in "
                "feed_health.error_bucket, not a missing remedy.",
        remedies=(REFRESH, PAUSE),
    ),
    "stale": Cause(
        label="Not being polled",
        why="Miniflux has not checked it in over a day",
        explain="Nothing is failing — the poll is not happening at all. This is "
                "ours to fix, not the publisher's, and it says nothing about "
                "whether they are still posting.",
        remedies=(REFRESH, RESUME),
    ),
    "quiet": Cause(
        label="Publisher has gone quiet",
        why="polling fine, but silent well past its own cadence",
        explain="The question you actually ask about a feed: has it stopped, or "
                "is it just slow this week? Measured against the feed's own "
                "median gap, because a feed that posts hourly is silent at six "
                "hours and a monthly one is not.",
        remedies=(REFRESH, MUTE_QUIET, PAUSE),
    ),
    "paused": Cause(
        label="Polling paused",
        why="paused by hand",
        explain="Deliberately off the schedule. Entries are kept and the feed "
                "record stays as the attachment point for a later import.",
        remedies=(RESUME,),
    ),
}

_UNKNOWN = Cause(
    label="Unclassified",
    why="a failure the classifier has no name for yet",
    explain="Miniflux reported something feed_health has never seen. The raw "
            "error is the only diagnosis until a case is added for it.",
)


def remedies_for(bucket: str) -> tuple[Remedy, ...]:
    cause = CAUSES.get(bucket)
    return cause.remedies if cause else ()


def cause_for(bucket: str) -> Cause:
    """The cause for a bucket, or a neutral placeholder for one we do not know."""
    return CAUSES.get(bucket) or _UNKNOWN


@dataclass(frozen=True)
class CauseGroup:
    bucket: str
    state: str                  # worst health state seen in this group
    label: str
    why: str
    explain: str
    remedies: tuple[Remedy, ...]
    feeds: list[dict]

    @property
    def count(self) -> int:
        return len(self.feeds)


def _worst(states) -> str:
    return min(states, key=lambda s: STATE_ORDER.index(s) if s in STATE_ORDER
               else len(STATE_ORDER))


def group_by_cause(feeds) -> list[CauseGroup]:
    """Gather annotated feeds into one group per cause, worst and largest first.

    Healthy feeds are dropped: a triage list is a list of things to answer.
    Input order is preserved inside each group, so whatever ordering the caller
    chose for the feed list survives into the group.
    """
    buckets: dict[str, list[dict]] = {}
    for feed in feeds:
        if feed.get("_health", "ok") == "ok":
            continue
        buckets.setdefault(feed.get("_bucket") or "other", []).append(feed)

    groups = [
        CauseGroup(
            bucket=bucket,
            state=_worst([f.get("_health", "ok") for f in members]),
            label=cause_for(bucket).label,
            why=cause_for(bucket).why,
            explain=cause_for(bucket).explain,
            remedies=remedies_for(bucket),
            feeds=members,
        )
        for bucket, members in buckets.items()
    ]
    groups.sort(key=lambda g: (
        STATE_ORDER.index(g.state) if g.state in STATE_ORDER else len(STATE_ORDER),
        -g.count,
        g.label,
    ))
    return groups


# Every bucket the classifier can produce must be answerable here. Checked at
# import so a new bucket cannot ship as a silently empty triage group.
_UNANSWERED = set(BUCKET_LABELS) - {"ok"} - set(CAUSES)
if _UNANSWERED:
    raise RuntimeError(f"feed health buckets with no cause: {sorted(_UNANSWERED)}")
