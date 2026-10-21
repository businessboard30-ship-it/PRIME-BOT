-- Welcome Card Studio Phase 6a: "if their DMs are closed, post in the channel instead" (off by default). Additive and idempotent.
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS dm_fallback_to_channel BOOLEAN NOT NULL DEFAULT FALSE;
