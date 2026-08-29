# Screenshots

**Everything in these images is synthetic.** The six publications, their authors
and every article are invented for this purpose. That is not a stylistic choice:
this repo is public, and a screenshot of a reader is a screenshot of whatever that
reader holds — subscriptions, reading history, what you starred. None of that
belongs in a public repo, so the shots are taken against a throwaway stack that has
never seen a real feed.

## How they are produced

A disposable Compose stack (its own project name, its own ports, no shared volume)
runs Postgres + Miniflux + an nginx serving generated RSS and article pages. A
script subscribes the feeds, drives engagement through the app's **own** endpoints
— `/entries/{id}/event`, `/thumb`, `/toggle-star` — so the ranker learns from the
same event stream a person would produce, and Playwright drives Chromium at
1440×900 and 390×780.

Two details are deliberate:

- The article **pages** carry more text than the **feed** does. That asymmetry is
  the thing full-text extraction exists to close, so the demo has to reproduce it
  rather than assert it.
- Three articles are edited after their first extraction, which is what gives the
  "Changed" view and the version diff something real to show.

Nothing in the harness ships here — it is a few dozen lines of throwaway
scaffolding, and the images are the artifact worth keeping.

## The files

| File | What it shows |
|---|---|
| `reader-dark.png` | The three-pane reader, dark (the default) |
| `reader-light.png` | The same, light |
| `ranking.png` | Smart ordering with a "why this ranked" explanation open |
| `changed.png` | The unified diff between two versions of one article |
| `feeds.png` | Feed management: priority tiers, health, OPML |
| `mobile-list.png`, `mobile-article.png` | Single-pane mobile at 390×780 |
