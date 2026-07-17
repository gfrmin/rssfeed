# rssfeed — working notes

A single-user RSS reader that runs as a sidecar alongside Miniflux. See `README.md` for
what it does and how to configure it; this file is about working *in* the repo.

## Layout

- `sidecar/` — the application (FastAPI + Jinja2 + htmx, no JS framework)
  - `app/routes/` — `entries.py` (reader/list), `feeds.py` (management), `cookies.py`, `proxy.py`
  - `app/templates/` — `shell.html` is the three-pane reader shell; `base.html` the outer page
  - `static/` — `style.css` (hand-written), `app.js`, `mobile.js`, bundled `htmx.min.js`
  - `ranker/model.bdsl` — the preference model, run on the Credence engine
- `credence-docker/` — Dockerfile for the external Credence (Julia) ranker engine
- `docs/` — design/audit notes

## Commands

Everything Python goes through **uv** — the system Python has none of the dependencies:

```bash
cd sidecar && uv run pytest     # tests  (or: make test)
cd sidecar && uv run ruff check .   # lint (or: make lint)
```

`ruff check` is clean and should stay that way. The repo is **not** `ruff format`-ed —
running the formatter would rewrite ~600 lines of hand-styled code, so don't do it
casually; it's a deliberate, standalone change if ever wanted.

## Running it

- **Production**: a user systemd unit — `systemctl --user restart rssfeed-sidecar`.
  Don't run `run-sidecar.sh` at the same time; they'd fight over the port.
- **Development**: `./run-sidecar.sh` serves on :9145 with `--reload`. Postgres and
  Miniflux run as containers (`rssfeed_db_1`, `rssfeed_miniflux_1`); the sidecar does not.

## Worktrees

Branch work happens in worktrees under `~/git/worktrees/rssfeed/<name>`.

**Gotcha**: `sidecar/pyproject.toml` sources `credence-skin-client` from the *relative*
path `../../credence/apps/skin/clients/python`. That resolves outside a worktree, so
`uv run` fails there unless a `credence` symlink sits beside the worktree:

```bash
ln -sfn ~/git/credence ~/git/worktrees/rssfeed/credence
```

## Conventions

- Default branch is **master**, not main.
- Review by PR (`gh pr create`); stack PRs when one depends on another. Don't self-merge.
- **This repo is public.** Never commit feed/article content, screenshots of real
  articles, or secrets. Secrets live in gnome-keyring, not `.env` files in the repo.
- Prefer functional style where it fits the surrounding code.
- The ranker and embeddings are both **optional and fail-open** — if the Credence engine
  or Ollama is unreachable, the reader must still work (falls back to priority+recency).
  Preserve that property.

## Data notes

- Miniflux owns feeds/entries/read/star state; the sidecar owns snapshots, feed config,
  engagement events, ranker weights, and embeddings in the same Postgres database.
- `drop_legacy_tables.sql` is an **un-run** optional cleanup script. Those tables still
  hold data (`article_tags` ~41k rows, `read_events` ~2k). Dropping them destroys data —
  only with explicit approval.
- Broken or dead feeds are **not** disposable: entries are the product, and feed records
  are attachment points for a planned historical import. Prefer disabling to deleting.
