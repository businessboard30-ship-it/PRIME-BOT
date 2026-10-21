-- Welcome Card Studio Phase 2d: remember that the old single picture was copied into the gallery as picture 1,
-- so emptying the gallery later does not bring it back. Additive and idempotent. The old custom_bg_* columns stay.
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS gallery_migrated BOOLEAN NOT NULL DEFAULT FALSE;
