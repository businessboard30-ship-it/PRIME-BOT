-- Cat spawn timing: the hours (UTC) during which a server wants cats. Start 0 / end 24 = all day (the default,
-- so every existing server behaves exactly as before). Additive, idempotent.
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS active_start SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS active_end   SMALLINT NOT NULL DEFAULT 24;
