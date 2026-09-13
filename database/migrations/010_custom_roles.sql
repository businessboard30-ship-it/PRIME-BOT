-- Custom Role perk (see discord_bot/cogs/custom_role.py + config.py's
-- CUSTOM_ROLE_FEE_USD): one-time, per-user unlock. A row is inserted with
-- role_id/style fields NULL the moment payment is approved (the
-- entitlement), then filled in once the buyer actually runs the wizard
-- and creates/edits their role. Re-running the wizard later just UPDATEs
-- this same row — it does not re-charge or insert a second row.
CREATE TABLE IF NOT EXISTS discord_custom_roles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id BIGINT,
    user_id BIGINT NOT NULL,
    role_id BIGINT,
    base_name TEXT,
    font_style TEXT,
    color_hex TEXT,
    icon TEXT,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, COALESCE(clone_id, -1), user_id)
);

CREATE INDEX IF NOT EXISTS idx_discord_custom_roles_guild
    ON discord_custom_roles (guild_id, COALESCE(clone_id, -1));

-- Per-guild kill switch for /customroleadmin — server owners can turn the
-- whole feature off if they don't want paying members' custom roles
-- visually outranking others, without affecting any other guild.
CREATE TABLE IF NOT EXISTS discord_custom_role_settings (
    guild_id BIGINT NOT NULL,
    clone_id BIGINT,
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, COALESCE(clone_id, -1))
);
