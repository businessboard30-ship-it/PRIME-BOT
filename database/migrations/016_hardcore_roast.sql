-- Migration 016: Hardcore roast per-battle activation
--
-- discord_hardcore_roast_pending tracks a challenger's payment + target
-- consent state before a hardcore battle actually starts.
-- discord_roast_battles gets a 'hardcore' boolean so on_message knows
-- which punchline bank to pull from.

CREATE TABLE IF NOT EXISTS discord_hardcore_roast_pending (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    clone_id        BIGINT,
    challenger_id   BIGINT NOT NULL,
    target_id       BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    -- awaiting_payment → awaiting_consent → accepted / declined / cancelled
    status          TEXT NOT NULL DEFAULT 'awaiting_payment',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hc_roast_pending_challenger
    ON discord_hardcore_roast_pending (challenger_id, status);

-- Add hardcore flag to existing roast battles table.
-- Default FALSE so existing rows are unaffected.
ALTER TABLE discord_roast_battles
    ADD COLUMN IF NOT EXISTS hardcore BOOLEAN NOT NULL DEFAULT FALSE;
