-- Welcome Card Studio Phase 6b: separate welcome text for DM delivery. NULL = use the normal message. Additive and idempotent.
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS dm_message_template TEXT;
