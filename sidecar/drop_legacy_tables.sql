-- Optional cleanup for the "back to basics" cleanup (2026-06).
--
-- The sidecar no longer creates or uses these tables. They are left in place on
-- existing databases (harmless orphans) so no data is destroyed automatically.
-- Run this by hand ONLY if you want to reclaim the space:
--
--   psql "$DATABASE_URL" -f drop_legacy_tables.sql
--
-- NOTE: these hold derived/secondary data only (LLM tags, embeddings, summaries,
-- read-event stats, saved filters/searches, share links). Feed/entry data and the
-- extracted article snapshots (article_snapshots) are NOT touched.

DROP TABLE IF EXISTS article_tags;
DROP TABLE IF EXISTS article_embeddings;
DROP TABLE IF EXISTS summary_prompts;
DROP TABLE IF EXISTS read_events;
DROP TABLE IF EXISTS saved_filters;
DROP TABLE IF EXISTS saved_searches;
DROP TABLE IF EXISTS share_links;
