# RSS Sidecar

A self-hosted, single-user RSS reader that runs as a sidecar alongside [Miniflux](https://miniflux.app). It adds features Miniflux intentionally excludes: full-text extraction, article versioning with content diffing, LLM-powered summaries, and a rich reading experience.

## Features

**Reading experience**
- Keyboard shortcuts (`j`/`k` navigate, `m` mark read, `s` star, `o` open, `?` help)
- Dark/light theme toggle
- Compact, normal, and expanded view modes
- Estimated reading time
- Podcast/audio player for feed enclosures
- Mobile swipe gestures (left = mark read, right = star)

**Feed management**
- Feed priority tiers (Must Read / Normal / Low)
- Category view with unread counts
- Feed favicons
- Feed health dashboard (stale/broken feed detection)
- OPML import/export
- Per-feed extract rules (XPath selectors, tag manipulation)

**Content extraction**
- Full-text article fetching with [trafilatura](https://github.com/adbar/trafilatura) + [readability-lxml](https://github.com/buriy/python-readability)
- Brightdata proxy fallback for paywalled/blocked sites
- Wayback Machine fallback as last resort
- Image proxying (avoids tracking pixels and broken hotlinks)
- Article versioning with unified diff view — track how articles change over time

**Search and filtering**
- Full-text search (via Miniflux API)
- Time-filtered views (today, last 24h, this week)
- Saved filter rules with auto-actions (mark read, star)
- LLM-generated topic tags with tag cloud filtering

**LLM integration (Ollama)**
- Article summarization (2-3 sentence summaries)
- Auto-tagging/classification
- Embedding-based duplicate and similarity detection
- Per-feed toggle for LLM features

**Sharing and export**
- Export articles to Markdown with YAML frontmatter (Obsidian-compatible)
- Time-limited public share links
- Reading statistics (daily/weekly charts, most-read feeds)

**Self-hosted friendly**
- All assets bundled locally (no CDN dependencies)
- PWA with service worker for offline reading
- Browser notifications for new articles
- Single Docker Compose stack

## Architecture

```
┌──────────┐     ┌──────────┐     ┌────────────┐
│ Miniflux │◄───►│ Postgres │◄───►│  Sidecar   │
│ :9144    │     │          │     │  :9145     │
└──────────┘     └──────────┘     └──────┬─────┘
                                         │
                                    ┌────▼─────┐
                                    │  Ollama  │
                                    │ (host)   │
                                    └──────────┘
```

The sidecar is a FastAPI + htmx application that:
- Uses Miniflux's API for feed/entry management
- Stores its own data (snapshots, tags, filters, stats) in the shared PostgreSQL database
- Runs a background worker that auto-extracts articles and runs LLM tasks
- Connects to Ollama on the host for summarization, tagging, and embeddings

## Setup

### Prerequisites
- Docker and Docker Compose
- [Ollama](https://ollama.ai) running on the host (optional, for LLM features)

### Quick start

```bash
# Clone the repo
git clone https://github.com/gfrmin/rssfeed.git
cd rssfeed

# Configure
cp .env.example .env
# Edit .env — set at minimum:
#   MINIFLUX_ADMIN_PASSWORD (something secure)
#   MINIFLUX_API_KEY (generate after first login, see below)

# Start
docker compose up -d

# 1. Open Miniflux at http://localhost:9144, log in with admin/your-password
# 2. Go to Settings → API Keys → Create a new API key
# 3. Add the key to .env as MINIFLUX_API_KEY
# 4. Restart: docker compose restart sidecar
# 5. Open the sidecar at http://localhost:9145
```

### Ollama setup (optional)

For LLM summarization, tagging, and similarity detection:

```bash
# Install Ollama: https://ollama.ai
ollama pull llama3.2          # for summarization and tagging
ollama pull nomic-embed-text  # for embeddings

# The sidecar connects to Ollama via host.docker.internal:11434 by default
# Customize with OLLAMA_URL, OLLAMA_MODEL, OLLAMA_EMBED_MODEL in .env
```

Then enable per-feed in the sidecar: go to a feed's settings page and toggle "LLM Summarization" on.

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `miniflux` | PostgreSQL password |
| `MINIFLUX_ADMIN_USER` | `admin` | Miniflux admin username |
| `MINIFLUX_ADMIN_PASSWORD` | `changeme` | Miniflux admin password |
| `MINIFLUX_API_KEY` | (required) | Miniflux API key |
| `BRIGHTDATA_PROXY` | | HTTP proxy URL for fetching blocked content |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama API URL |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model for summarization/tagging |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama model for embeddings |

## Per-feed extract rules

For sites where automatic extraction doesn't work well, you can set custom rules in each feed's settings page (JSON):

```json
{
  "content_xpath": "//article//div[@class='post-body']",
  "unwrap_tags": ["template"],
  "remove_tags": ["widget-*", "related-posts"]
}
```

- `content_xpath` — XPath selector for the main content element
- `unwrap_tags` — HTML tags to unwrap (promote children), useful for Vue.js/Web Component sites
- `remove_tags` — Glob patterns for tags to remove entirely

## Tech stack

- **Backend**: Python 3.12, FastAPI, psycopg3
- **Frontend**: htmx, vanilla JS, CSS custom properties
- **Database**: PostgreSQL 17 (shared with Miniflux)
- **Extraction**: trafilatura, readability-lxml, lxml
- **LLM**: Ollama (local, self-hosted)
- **Containerization**: Docker Compose

## License

[GNU Affero General Public License v3.0](LICENSE)
