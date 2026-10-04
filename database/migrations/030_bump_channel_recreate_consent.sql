-- Bump channel deleted -> ask before creating a new one (discord_bot/cogs/bump.py _ask_to_recreate).
-- asked_at: we already sent the question for this deletion (so we ask once, then stay quiet).
-- declined: the owner said no; the server stops receiving bumps until /bumpsetup is run again.
-- Both are reset automatically when a bump channel is set again. Additive and idempotent.
ALTER TABLE bump_guild_config ADD COLUMN IF NOT EXISTS channel_recreate_asked_at TIMESTAMPTZ;
ALTER TABLE bump_guild_config ADD COLUMN IF NOT EXISTS channel_recreate_declined BOOLEAN NOT NULL DEFAULT FALSE;
