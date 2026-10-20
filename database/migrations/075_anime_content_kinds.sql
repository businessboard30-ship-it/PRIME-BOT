-- Anime feed, slice D4: what a server's feed posts. 'anime' | 'donghua' | 'both'. Donghua is ON by default, existing servers included
-- (the column default fills every existing row with 'both'); an admin can switch to anime-only in the setup wizard. Additive and idempotent.
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS content_kinds TEXT NOT NULL DEFAULT 'both';
