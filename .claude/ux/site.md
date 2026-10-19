# Site context: rssfeed (sidecar reader over Miniflux)

A single-user RSS reader: Miniflux polls and stores feeds; the sidecar (this repo) is the whole UI.
Read by the global `ux-loop` skill and its critics. Repo rules are in `CLAUDE.md`.

## Run

- **Working directory**: `sidecar/`
- **Command**: `../run-sidecar.sh` from the repo root of a checkout with a `.env`; it serves `--reload` on
  `RSSFEED_PORT` (default 9145, loopback). It needs Postgres and Miniflux running (containers `rssfeed_db_1`,
  `rssfeed_miniflux_1`); the sidecar itself is not containerised.
- **Port / health**: 9145; `curl -sf http://localhost:9145/ -o /dev/null` (no `/health` route; `/` is the
  feeds table, so it is also the slowest page)
- **Env / fixtures**: the ranker (`CREDENCE_SKIN_SERVER`) and embeddings (Ollama) are optional and fail open:
  with neither, cross-feed views order by priority + recency. Run critiques with both off unless the round is
  about ranking.
- **Local data**: **none yet** — a seeded instance with invented feeds and articles is loop stage 4. Until it
  exists, nothing captured from a running instance may be committed (see Constraints).
- **Prod fallback**: the owner's instance (host, address and access in the private ops repo, `rssfeed/README.md`), **GETs
  only**, a few pages.
  It holds the owner's real subscriptions and articles: crops stay in the scratchpad and are described, never
  committed. Say in the report that prod was used.
- **Never run**: `run-sidecar.sh` on a host where the `rssfeed-sidecar` user unit is running (they fight
  over the port); any POST against prod (every POST changes feeds, read state or polling); `ruff format`.

## Base URLs and locales

| locale | path prefix | lang / dir | notes |
|---|---|---|---|
| en | `/` | `en` / `ltr` | only locale; article *content* can be any language or direction (Hebrew, Chinese) |

## Routes to sample

- **How to get real ids**: `podman exec -i rssfeed_db_1 psql -U miniflux -d miniflux -At` on the host running the
  instance, with `SELECT id FROM feeds WHERE NOT disabled ORDER BY checked_at DESC LIMIT 3` on stdin; entry ids
  the same way from `entries`. Triage buckets come from `/triage`'s own links.
- **Routes**: `/` (feeds table), `/entries?view=unread`, `/entries?view=all`, `/entries?feed_id={id}`,
  `/entries/{id}` (direct load, not only via htmx: a direct load is a journey failure mode),
  `/entries/{id}/diff` for an entry with versions (`/entries?view=changed` lists them), `/triage`,
  `/triage/{bucket}`, `/feeds/{id}`, `/cookies`, `/entries?search=<real term>` and a no-hit term.
  404: `/nope`.
- **Dated routes**: none.

## Route enumeration (journey traceability)

`grep -rhoE '@(router|app)\.(get|post|put|delete)\("[^"]+"' sidecar/app` — blind spot: htmx fragments are
the same routes returning partials when `HX-Request` is set, so a route can exist and still render wrong
when loaded directly. Journey `surface` value: `sidecar`.

## UX paths

- **DESIGN.md**: none yet (loop stage 4). Earlier design work: `docs/design/reachability-triage/`,
  `docs/superpowers/specs/2026-04-02-warm-editorial-redesign.md`, `docs/mobile-ux-audit.md`.
- **Ledger**: none yet; will be `docs/ux/findings.yaml`
- **Journeys**: `.claude/ux/journeys/` (nine, all evidence-backed; one kill signal met:
  `teach-what-i-like`). Surface `sidecar`.
- **Gate test**: none yet — the `ux-loop` skill describes the ledger gate; write it with the ledger
- **Committed crops**: none until the seeded instance exists; then `docs/ux/crops/`, sev≥3 only, ≤150 KB
- **Signals reader**: none; run each `sql` signal through `podman exec -i rssfeed_db_1 psql -U miniflux -d miniflux -At`
  on the instance's host with the query on stdin (over ssh, `-c` quoting breaks), and `log` signals there as
  written.

## Tests

- **Command**: `cd sidecar && uv run pytest`
- **Skip ceiling**: 6 (the extraction corpus blobs, absent unless `tests/fixtures/extraction/fetch_fixtures.py`
  has been run)

## Constraints

- **Public repo.** No feed names, article text, titles or screenshots from a real instance in commits, PR
  bodies or the ledger. Crops are committed only from the seeded instance. Journeys cite the owner's prompt
  history by date, de-identified.
- **Prod is read-only for the loop.** Every POST mutates the owner's reading state or Miniflux.
- **Fail-open must hold.** A finding fixed by making the reader depend on the ranker or Ollama is not fixed.

## Lenses

- **Stack**: FastAPI + Jinja2 + htmx (no JS framework); `shell.html` is the three-pane reader shell,
  `base.html` the outer page; `static/style.css` (hand-written, unlayered), Tailwind v4 build
  (`static/tailwind.css`, utilities in `@layer utilities`), `app.js`, `mobile.js`, `palette.js`.
  **Cascade trap**: an unlayered element rule in `style.css` beats any Tailwind utility regardless of
  specificity; element defaults belong in `@layer base` (locked by
  `tests/test_static_assets.py::test_link_defaults_sit_below_tailwind_utilities`).
- **Device**: Android first. The owner reads on an Android phone in Brave over a VPN and uses no Apple
  hardware: emulate Pixel/Android at 360–412px, never iPhone. Mobile layout is CSS-owned at
  `max-width: 879px`; `app.js` mirrors it via `matchMedia`. Swipe (finger-follow) moves between posts;
  Android back must retrace reader → list → feeds.
- **Theme**: dark by default (`:root, [data-theme="dark"]`); check both themes.
- **Colour**: red/green colour-blind safe — state is icon + word (+ ▲/▼), never hue alone.
- **No native dialogs**: an `alert()`/`confirm()` is a finding (owner rejected them, 2026-04-21). Three
  `hx-confirm` attributes still raise one: repoint confirm (`feed_discover_preview.html`), cookie delete
  (`cookies.html`), feed delete (`feed_settings.html`).
- **Every action answers**: a button whose effect is invisible until reload is sev 3+ by default; it is the
  owner's single most repeated complaint.
- **Direction / locales**: UI is LTR English; article bodies may be RTL (Hebrew) or CJK — check an RTL
  article's text direction and that its punctuation does not jump.
- **Dataviz**: generic rubric only (the warmth meter and feed health dots are the only graphics).
- **Data correctness**: relative times ("3h ago") are rendered server-side (`app/templating.py:_timeago`, UTC);
  an absolute time must not silently show UTC to a reader in another zone. "Stale" today
  means last *poll*, not last *post* (known, see the triage design README). Read state is Miniflux's; a row's
  bold/unbold must match it after back-navigation.

## Live controls

| finding id | check | route | viewport | fixed by |
|---|---|---|---|---|
| none | | | | |
