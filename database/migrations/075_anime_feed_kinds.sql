-- path: database/migrations/075_anime_feed_kinds.sql

-- Anime feed D4: what a server's feed posts. 'both' (anime + donghua) is the default, so servers already using the feed
-- start receiving donghua with no admin action. 'anime' = everything except Chinese animation, 'donghua' = only Chinese animation.
-- Additive and idempotent.
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS content_kinds TEXT NOT NULL DEFAULT 'both';
