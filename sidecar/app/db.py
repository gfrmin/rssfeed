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

-- Per-feed reading preferences (Part A): default to showing read articles on the
-- feed's own page, and per-feed author/tag mute lists (hidden on the feed page and
-- down-ranked in cross-feed smart views).
ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS show_read_default BOOLEAN DEFAULT FALSE;
ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS author_mutes JSONB DEFAULT '[]'::jsonb;
ALTER TABLE feed_config ADD COLUMN IF NOT EXISTS tag_mutes JSONB DEFAULT '[]'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_entry_hash
  ON article_snapshots(entry_id, content_hash);

-- Track RSS source content hash for change detection
ALTER TABLE article_snapshots ADD COLUMN IF NOT EXISTS source_hash TEXT;

-- Domain-level cookies for paywalled sites
CREATE TABLE IF NOT EXISTS site_cookies (
    domain TEXT PRIMARY KEY,
    cookies JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- History of feed URL changes so imported legacy posts (e.g. from NewsBlur)
-- can be matched to current feeds via their old URL.
CREATE TABLE IF NOT EXISTS feed_url_history (
    id BIGSERIAL PRIMARY KEY,
    feed_id BIGINT NOT NULL,
    old_url TEXT NOT NULL,
    new_url TEXT NOT NULL,
    source TEXT,
    changed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feed_url_history_feed ON feed_url_history(feed_id);
CREATE INDEX IF NOT EXISTS idx_feed_url_history_old_url ON feed_url_history(old_url);

-- Engagement signals (Part B): quality-of-attention events that feed the
-- learning ranker (Part C). Deliberately NOT the deleted read_events table —
-- plain reads from swiping or "mark all read" are never recorded here; only
-- signals that distinguish interest (star, thumbs, opening the original,
-- dwelling on an article) are.
CREATE TABLE IF NOT EXISTS engagement_events (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL,
    feed_id BIGINT,
    signal TEXT NOT NULL,          -- star | unstar | thumb_up | thumb_down
                                   -- | open_original | dwell
    value DOUBLE PRECISION,        -- dwell seconds; +/-1 for thumbs; 1 otherwise
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_engagement_entry ON engagement_events(entry_id);
CREATE INDEX IF NOT EXISTS idx_engagement_created ON engagement_events(created_at);

-- Learned ranker posterior (Part C). A single row holds the serialized weight
-- beliefs (plain conjugate params as JSON, not credence's binary format) plus a
-- high-water mark of the last engagement_events.id folded into the model. Python
-- owns this table; the Julia runner is fed from / writes back to it.
CREATE TABLE IF NOT EXISTS ranker_state (
    id            SMALLINT PRIMARY KEY DEFAULT 1,
    model_version TEXT NOT NULL,
    state_blob    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"weights":{name:[p,q]}, "obs_count":N}
    last_event_id BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    CHECK (id = 1)
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


def run_migrations() -> None:
    with get_sync_conn() as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()
