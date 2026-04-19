import contextlib
from collections.abc import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feed_config (
    feed_id BIGINT PRIMARY KEY,
    fetch_full_content BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS article_snapshots (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL,
    feed_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    content_text TEXT,
    content_html TEXT,
    content_hash TEXT,
    metadata JSONB,
    version INT DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_snapshots_entry ON article_snapshots(entry_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_feed ON article_snapshots(feed_id);

ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS priority INT DEFAULT 2;

ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS extract_rules JSONB DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_entry_hash
  ON article_snapshots(entry_id, content_hash);

-- Read events for statistics
CREATE TABLE IF NOT EXISTS read_events (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL,
    feed_id BIGINT NOT NULL,
    read_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_read_events_date ON read_events(read_at);
CREATE INDEX IF NOT EXISTS idx_read_events_feed ON read_events(feed_id);

-- Saved filters / rules
CREATE TABLE IF NOT EXISTS saved_filters (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    rules JSONB NOT NULL DEFAULT '[]',
    auto_action TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Article tags (LLM-generated)
CREATE TABLE IF NOT EXISTS article_tags (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL,
    tag TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_article_tags_entry ON article_tags(entry_id);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);

-- Share links
CREATE TABLE IF NOT EXISTS share_links (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_share_links_token ON share_links(token);

-- Article embeddings for duplicate/similarity detection
CREATE TABLE IF NOT EXISTS article_embeddings (
    entry_id BIGINT PRIMARY KEY,
    embedding FLOAT8[] NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Drop obsolete summarize flag (summarisation is now on-demand per article)
ALTER TABLE feed_config DROP COLUMN IF EXISTS summarize;

-- Named summarisation prompts (built-in presets + user-saved)
CREATE TABLE IF NOT EXISTS summary_prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Track RSS source content hash for change detection
ALTER TABLE article_snapshots ADD COLUMN IF NOT EXISTS source_hash TEXT;

-- Domain-level cookies for paywalled sites
CREATE TABLE IF NOT EXISTS site_cookies (
    domain TEXT PRIMARY KEY,
    cookies JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def get_sync_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextlib.asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        yield conn


BUILTIN_PROMPTS = [
    (
        "default",
        "Concise",
        "You are a concise article summarizer. Output ONLY the summary itself — "
        "2-3 sentences stating the key facts and takeaways. "
        "Do not include any preamble, meta-commentary, or framing. "
        "Never begin with phrases like 'Here is', 'This article', 'The article', "
        "'In summary', 'Summary:', or similar. Start directly with the content.",
    ),
    (
        "bullets",
        "Bullet points",
        "You are an article summarizer. Output 3-6 terse bullet points covering the key facts and takeaways. "
        "Each bullet on its own line, prefixed with '- '. No preamble, no headings, no closing commentary. "
        "Start directly with the first bullet.",
    ),
    (
        "eli5",
        "ELI5",
        "You are explaining an article to a curious non-expert. In 2-4 short sentences, convey the main idea "
        "in plain, everyday language. Avoid jargon; if you must use a technical term, briefly define it inline. "
        "No preamble, no 'Here is', no framing — start directly.",
    ),
    (
        "skeptic",
        "Skeptic's take",
        "You are a skeptical reader. In 2-4 sentences, summarise the article's central claim and then flag the "
        "weakest assumption, unstated caveat, or missing evidence a careful reader should question. "
        "No preamble, no framing — start directly with the claim.",
    ),
]


def _seed_builtin_prompts(conn: psycopg.Connection) -> None:
    for slug, name, system_prompt in BUILTIN_PROMPTS:
        conn.execute(
            "INSERT INTO summary_prompts (id, name, system_prompt, is_builtin) "
            "VALUES (%s, %s, %s, TRUE) ON CONFLICT (id) DO NOTHING",
            (slug, name, system_prompt),
        )


def run_migrations() -> None:
    with get_sync_conn() as conn:
        conn.execute(SCHEMA_SQL)
        _seed_builtin_prompts(conn)
        conn.commit()
