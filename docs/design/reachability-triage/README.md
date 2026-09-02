# Reachability & feed-error triage — design canvas

Three artboards exploring two related problems in the sidecar:

1. **Reachability** — most of what `routes/feeds.py` can already do is four clicks
   deep behind `/`, and the reader shell has no keyboard route to any of it.
2. **Triage** — `feed_health.py` already classifies failures into buckets
   (`bot_blocked`, `auth`, `dns_fail`, `not_a_feed`, …), but the UI renders that
   work as a 10px label with the raw error hidden in a `title` attribute.

## The artboards

| File | What it is |
| --- | --- |
| `Main.dc.html` | **The deliverable.** Triage as a view inside the existing three-pane shell: sidebar picks a cause, list shows its feeds, reader pane holds the ordered remedies. |
| `Palette.dc.html` | **⌘K command palette** over the reader. Standard Linear/Raycast convention; covers only actions that have no key today. `j/k/o/v/m/s/r` are untouched. |
| `FeedsPage.dc.html` | **The alternative.** `/feeds` restyled into the dense system, health dropdown promoted to cause chips, bulk bar always visible. Cheaper, but no room for per-cause explanation. |
| `Paths.dc.html` | **Wayfinding.** A route map of how you reach a feed's settings, flippable between TODAY and PROPOSED. |

`canvas.json` lays them out and carries the sticky notes, including the tradeoff
between Main and FeedsPage.

## The idea underneath

Grouping is borrowed from error monitoring (Sentry's group-by-signature), not
invented: on a real snapshot of this instance, **19 of 21 failing feeds were two
causes wearing 19 names**. A remedy that binds to a feed row makes you fix the
same problem nineteen times; one that binds to the cause group does not.

## Badges

- `EXISTS` (solid) — the endpoint is already in `routes/feeds.py`. Pure reachability change.
- `PROPOSED` (dashed amber) — new behaviour, nothing built.

## The wayfinding dead end

The settings overlay at `/feeds/{id}` is reachable from exactly three places:
the gear in `_reader.html:32`, the "Subscription login" link in
`_content_block.html` (only when full text looks truncated), and the gear in the
`/feeds` table — a full page load out of the reader, not the overlay.

It is **not** reachable from the sidebar feed row, which is where the health dot
lives; nor from `_list.html`'s header when you are scoped to one feed; nor from
any key. So the dot says "this feed is broken" and the only route that stays
inside the reader is to open an article from that feed — which, being broken,
has no new articles.

## Two findings worth acting on regardless

- **`stale` measures the wrong thing.** `_macros.html` labels it *"no items 24h+"*,
  but `feed_health.py:classify` derives `is_stale` from `checked_at` — when Miniflux
  last *polled*, not when the publisher last *posted*. Nothing tracks publisher
  silence, which is the question you actually ask of a quiet feed.
- **`bot_blocked` is too coarse.** `error_bucket()` maps both a Cloudflare JS
  interstitial and a plain `403` to one bucket, but they need different remedies.

## Working on these files

The artboards are [Claude Design](https://claude.ai/code/artifact/c48a9602-73bc-4f86-a570-3888b040805a)
components. Edit the `.dc.html` files, then re-seed and republish:

```bash
node "$DESIGN_SKILL/seed-canvas.mjs" \
  --template "$DESIGN_SKILL/payload.template.html" \
  --out feed-triage-and-command-palette.html \
  --title "Feed Triage and Command Palette" \
  --artboard Main.dc.html --artboard Palette.dc.html \
  --artboard FeedsPage.dc.html --artboard Paths.dc.html \
  --canvas canvas.json
```

The seeded `feed-triage-and-command-palette.html` is ~2.3 MB of bundled editor and
is **not** committed — see `.gitignore`. Regenerate it with the command above.

## Data in these mockups

Feed names, URLs and titles are **synthetic** (`*.example` domains), per the repo's
rule against committing feed content. The failure counts and error strings are real,
taken from a Miniflux log snapshot: 9 Cloudflare challenges, 10 plain 403s, 1 auth
failure, 1 timeout.
