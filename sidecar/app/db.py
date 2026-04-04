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

-- Feed icon cache
CREATE TABLE IF NOT EXISTS feed_icons (
    feed_id BIGINT PRIMARY KEY,
    icon_data BYTEA,
    icon_mime TEXT,
    fetched_at TIMESTAMPTZ DEFAULT NOW()
);

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

-- LLM summarize flag on feed_config
ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS summarize BOOLEAN DEFAULT FALSE;

-- Track RSS source content hash for change detection
ALTER TABLE article_snapshots ADD COLUMN IF NOT EXISTS source_hash TEXT;
"""


def get_sync_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextlib.asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        yield conn


def run_migrations() -> None:
    with get_sync_conn() as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()
