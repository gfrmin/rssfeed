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
