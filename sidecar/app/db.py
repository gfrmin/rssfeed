import contextlib
import logging
from collections.abc import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

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
    state_blob    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"weights":{name:{"type":"gaussian","mu":..,"sigma":..}}, "obs_count":N}
    last_event_id BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    CHECK (id = 1)
);

-- Article embeddings + taste centroid (Part C phase 2). The worker embeds article
-- text via Ollama; embed_sim = cosine(article, centroid) is one more ranker feature.
CREATE TABLE IF NOT EXISTS entry_embeddings (
    entry_id   BIGINT PRIMARY KEY,
    model      TEXT NOT NULL,
    vec        JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- The taste centroid: mean embedding of positively-engaged articles.
CREATE TABLE IF NOT EXISTS ranker_taste (
    id         SMALLINT PRIMARY KEY DEFAULT 1,
    centroid   JSONB,
    n          INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (id = 1)
);

-- Mutes as ranker evidence: muting an author/tag on a feed also logs a negative
-- engagement event so the cross-feed ranker learns the aversion (it previously
-- only hard-hid/sank muted items without learning). These rows describe a
-- config action, not an article: entry_id is optional provenance and `detail`
-- carries the muted author/tag string the observation is built from.
ALTER TABLE engagement_events ALTER COLUMN entry_id DROP NOT NULL;
ALTER TABLE engagement_events ADD COLUMN IF NOT EXISTS detail TEXT;
"""

# Vector storage, kept OUT of SCHEMA_SQL so a plain postgres:17 deployment still boots.
# It needs the pgvector extension (image: pgvector/pgvector:pg17); when that's missing
# this block fails, VECTOR_READY stays False, and every embedding-backed feature simply
# switches off instead of breaking the reader.
#
# `emb` supersedes the legacy `vec` JSONB column: 3KB/row instead of ~10KB, and an HNSW
# index answers nearest-neighbour queries in single-digit ms instead of a full scan. The
# denormalized feed_id/title/published_at let /related render without calling Miniflux.
# `vec` is deliberately left in place (unwritten) for one release, so a rollback keeps
# its data; a follow-up migration drops it.
VECTOR_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE entry_embeddings ADD COLUMN IF NOT EXISTS emb vector(768);
ALTER TABLE entry_embeddings ADD COLUMN IF NOT EXISTS feed_id BIGINT;
ALTER TABLE entry_embeddings ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE entry_embeddings ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

-- `vec` was NOT NULL back when it was the only storage. New rows write `emb` and
-- leave `vec` empty, so the constraint has to go or every insert fails.
ALTER TABLE entry_embeddings ALTER COLUMN vec DROP NOT NULL;

-- One-time conversion of already-embedded rows. Guarded on `vec` still existing so
-- this stays valid after the follow-up drop, and on the 768-dim shape so a row from
-- a different model can't produce a dimension error.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'entry_embeddings' AND column_name = 'vec'
    ) THEN
        UPDATE entry_embeddings
           SET emb = (ARRAY(SELECT jsonb_array_elements_text(vec)::float4))::vector(768)
         WHERE emb IS NULL
           AND jsonb_array_length(vec) = 768;
    END IF;
END $$;

-- Seed the denormalized render columns for rows embedded before they existed.
-- Miniflux shares this database, so one UPDATE does what would otherwise be 8k+ REST
-- calls. Without it /related renders nothing until the archive sweep happens to reach
-- each row — and since that sweep runs oldest-id first, the newest articles (the ones
-- actually being read) would be the last to work.
--
-- Guarded on the table existing: if the sidecar ever points at a database that isn't
-- Miniflux's, this must skip quietly rather than fail the whole vector block and
-- switch the feature off.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'entries'
    ) THEN
        UPDATE entry_embeddings ee
           SET title        = e.title,
               feed_id      = e.feed_id,
               published_at = e.published_at
          FROM entries e
         WHERE e.id = ee.entry_id
           AND ee.title IS NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_entry_embeddings_emb
  ON entry_embeddings USING hnsw (emb vector_cosine_ops);

-- Resumable cursor for the backfill pass that embeds the archive.
CREATE TABLE IF NOT EXISTS embed_backfill (
    id              SMALLINT PRIMARY KEY DEFAULT 1,
    cursor_entry_id BIGINT NOT NULL DEFAULT 0,
    done            BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    CHECK (id = 1)
);
"""

# True once VECTOR_SCHEMA_SQL has applied cleanly. Everything vector-backed gates on
# `EMBED_ENABLED and db.VECTOR_READY`.
VECTOR_READY = False


def get_sync_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextlib.asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        yield conn


def run_migrations() -> None:
    global VECTOR_READY
    with get_sync_conn() as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()
    # Separate transaction: a database without pgvector must still come up with the
    # core schema applied and the reader fully working, minus similarity features.
    try:
        with get_sync_conn() as conn:
            conn.execute(VECTOR_SCHEMA_SQL)
            conn.commit()
        VECTOR_READY = True
    except psycopg.Error as exc:
        VECTOR_READY = False
        logger.warning(
            "pgvector unavailable — embeddings and related-articles are disabled "
            "(use image pgvector/pgvector:pg17 to enable them): %s",
            exc,
        )
