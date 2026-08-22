-- Reference copy of the schema database.py's _create_tables() creates
-- automatically on cold start. You do not need to run this by hand against
-- a normal deployment — it's here for anyone who prefers to provision the
-- database manually, and as documentation of the shape.
--
-- Supersedes the single-tier sql/discord_guild_premium.sql: a guild can now
-- have any number of independently-priced premium groups instead of
-- exactly one. Existing discord_guild_premium rows are folded into this
-- table automatically (see the WHERE NOT EXISTS backfill in database.py).

CREATE TABLE IF NOT EXISTS discord_cloned_bots (
    clone_id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    bot_token_encrypted TEXT NOT NULL,
    bot_user_id BIGINT,
    bot_username TEXT,
    application_id BIGINT,
    status TEXT NOT NULL DEFAULT 'active',
    last_heartbeat TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_cloned_bots_owner ON discord_cloned_bots (owner_id);
CREATE INDEX IF NOT EXISTS idx_discord_cloned_bots_status ON discord_cloned_bots (status);

-- Discord auto-mod (Phase 1 of the Discord expansion) — SUPERSEDED, see
-- discord_automod_config below. Left as a no-op historical record; nothing
-- in the codebase reads these columns anymore.
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS automod_action TEXT DEFAULT 'delete';
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS automod_timeout_minutes INTEGER DEFAULT 10;
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_invite_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_mention_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_mention_threshold INTEGER DEFAULT 5;
ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS min_account_age_hours INTEGER DEFAULT 0;

-- Discord-only auto-mod config (current). (guild_id, clone_id)-scoped, no FK
-- into Telegram's users table (unlike group_moderation_settings.admin_id).
CREATE TABLE IF NOT EXISTS discord_automod_config (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    action TEXT NOT NULL DEFAULT 'delete',
    timeout_minutes INTEGER NOT NULL DEFAULT 10,
    log_channel_id BIGINT,
    word_filter_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    banned_words JSONB NOT NULL DEFAULT '[]',
    anti_invite_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    anti_mention_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    anti_mention_threshold INTEGER NOT NULL DEFAULT 5,
    spam_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    spam_flood_threshold INTEGER NOT NULL DEFAULT 10,
    spam_flood_window_seconds INTEGER NOT NULL DEFAULT 10,
    min_account_age_hours INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_automod_config_guild_clone_key
    ON discord_automod_config (guild_id, COALESCE(clone_id, -1));

-- NOTE on clone_id below: these four tables originally shipped without
-- clone_id at all, which meant a clone and the main bot (or two clones)
-- running in the same guild silently shared reaction-role panels, XP,
-- level-role rewards, and welcome config — a direct violation of the
-- expansion spec's "no exceptions" clone-isolation rule. Fixed by adding
-- clone_id (NULL = main bot, matching discord_premium_groups) and widening
-- each table's uniqueness constraint to include it. No FK to
-- discord_cloned_bots on these four (unlike discord_premium_groups) because
-- in database.py's _create_tables() they're created before
-- discord_cloned_bots exists; integrity is enforced at the application
-- layer instead (clone_id always comes from an existing
-- discord_cloned_bots row via bot.clone_id).
CREATE TABLE IF NOT EXISTS discord_reaction_roles (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    label TEXT NOT NULL,
    emoji TEXT,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(message_id, role_id)  -- message_id is globally unique on Discord's side, so clone_id isn't needed here
);
CREATE INDEX IF NOT EXISTS idx_discord_reaction_roles_message ON discord_reaction_roles (message_id);
CREATE INDEX IF NOT EXISTS idx_discord_reaction_roles_guild ON discord_reaction_roles (guild_id, clone_id);

-- Phase 2: leveling / XP (ProBot parity) ------------------------------------
CREATE TABLE IF NOT EXISTS discord_xp (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    user_id BIGINT NOT NULL,
    total_xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    last_xp_at TIMESTAMPTZ
);
-- NULLs aren't equal in a plain UNIQUE constraint, so a COALESCE'd unique
-- index is used instead of a composite PRIMARY KEY, otherwise every
-- main-bot (clone_id IS NULL) row for the same (guild_id, user_id) would be
-- treated as distinct and duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS discord_xp_guild_clone_user_key
    ON discord_xp (guild_id, COALESCE(clone_id, -1), user_id);
CREATE INDEX IF NOT EXISTS idx_discord_xp_leaderboard ON discord_xp (guild_id, clone_id, total_xp DESC);

CREATE TABLE IF NOT EXISTS discord_level_roles (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    level INTEGER NOT NULL,
    role_id BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_level_roles_guild_clone_level_key
    ON discord_level_roles (guild_id, COALESCE(clone_id, -1), level);

-- Phase 2: welcome cards (ProBot parity) -------------------------------------
CREATE TABLE IF NOT EXISTS discord_welcome_config (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    channel_id BIGINT,
    message_template TEXT NOT NULL DEFAULT 'Welcome {member} to {guild}! You are member #{count}.',
    background_color TEXT NOT NULL DEFAULT '#2b2d31',
    accent_color TEXT NOT NULL DEFAULT '#5865F2',
    sticker_url TEXT DEFAULT 'https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif',
    card_style TEXT NOT NULL DEFAULT 'gif',
    avatar_shape TEXT NOT NULL DEFAULT 'circle',
    nudge_sent_at TIMESTAMPTZ,
    nudge_status TEXT,
    sticker_announced_at TIMESTAMPTZ,
    sticker_announce_status TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_welcome_config_guild_clone_key
    ON discord_welcome_config (guild_id, COALESCE(clone_id, -1));

-- Backfill for databases created before sticker/card-style/avatar-shape/nudge
-- columns existed — set_welcome_config's INSERT/UPDATE references all of
-- these, and their absence caused every wizard save (channel, message,
-- color, sticker, avatar shape) to fail with "something went wrong".
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_url TEXT DEFAULT 'https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif';
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS card_style TEXT NOT NULL DEFAULT 'gif';
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS avatar_shape TEXT NOT NULL DEFAULT 'circle';
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS nudge_sent_at TIMESTAMPTZ;
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS nudge_status TEXT;
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_announced_at TIMESTAMPTZ;
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_announce_status TEXT;

CREATE TABLE IF NOT EXISTS discord_clone_pending_payments (
    reference TEXT PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    bot_token_encrypted TEXT NOT NULL,
    bot_user_id BIGINT,
    bot_username TEXT,
    application_id BIGINT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_clone_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_clone_pending_payments_owner ON discord_clone_pending_payments (owner_id);

CREATE TABLE IF NOT EXISTS discord_premium_groups (
    group_id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
    name TEXT NOT NULL,
    role_id BIGINT NOT NULL,
    fee_ghs NUMERIC NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_premium_groups_guild ON discord_premium_groups (guild_id, clone_id);

ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS group_id INTEGER;
