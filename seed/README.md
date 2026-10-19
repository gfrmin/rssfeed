# Seeded instance

A complete rssfeed with invented content: 19 made-up publications served from
loopback, a throwaway Postgres + Miniflux, and a sidecar pointed at them. It is
what the UX loop audits and crops, because this repo is public and the real
instance holds someone's reading list.

```bash
make seed-up      # ~20 s once images are pulled; prints http://127.0.0.1:9147/
make seed-down    # stops everything; the database is tmpfs, so nothing persists
```

Needs podman + podman-compose, uv, and the `pgvector/pgvector:pg17` and
`miniflux/miniflux` images (the same ones `docker-compose.yml` uses).

## What it contains

| piece | file | notes |
|---|---|---|
| publications, entries, prose | `content.py` | pure data; deterministic per (feed, index) |
| the publishers' sites | `server.py` on :9148 | feeds, article pages, SVG images; `POST /_break` makes the failing feeds fail |
| Postgres :5434, Miniflux :9146 | `compose.yml` | separate project and ports from the real stack; tmpfs database |
| loading it | `seed.py` | phase 1 via the Miniflux API, phase 2 through the sidecar's own routes |
| sidecar :9147 | `run.sh` | clean environment: no keyring secrets, no proxy, ranker and embeddings off, `EGRESS_GUARD=0` so it may fetch loopback |

States it sets up, each named by a journey's failure modes (`.claude/ux/journeys/`):

- **Read/unread mix**: 78 of 206 entries read; two starred; thumbs and dwell events.
- **Full text**: a teaser-only feed with full-content on, so extraction runs; a
  paywalled one whose article page is a teaser; one article (`CHANGING`) whose
  page changes after the first fetch, giving two versions and a diff.
- **Triage**: one feed per cause — server error, 403, 404, HTML instead of XML,
  connection refused, DNS failure — each failed three times (persistent); a
  paused feed; a feed whose publisher went quiet 26 days ago.
- **Edge content**: a Hebrew (RTL) feed, a 60-entry feed that floods the
  ranked view, entities and markup in titles, very long names, an author and a
  tag mute, a feed with eight tags per post, a podcast enclosure.

Times are relative to when the server started, so "3h ago" means the same thing
on every run, but the absolute dates differ.

## Ports

Override with `SEED_SERVER_PORT`, `SEED_SIDECAR_PORT`, `SEED_DB_PORT`,
`SEED_MINIFLUX_PORT`. Logs and the generated API key go to
`$XDG_RUNTIME_DIR/rssfeed-seed/`.
