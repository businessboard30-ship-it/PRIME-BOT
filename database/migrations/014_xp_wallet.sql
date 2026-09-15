-- path: database/migrations/014_xp_wallet.sql

-- ============================================================================
-- Migration 014: Boost Wallet (flat, giftable XP currency)
-- ============================================================================
-- Separate from discord_xp_boosts (013) — that table is a temporary
-- multiplier+duration ("2x for 7 days"); this table is a spendable BALANCE
-- of flat XP a member bought and can gift to someone else. Gifting moves
-- balance out of the sender's wallet and lands as real XP directly on the
-- recipient's discord_xp.total_xp (see database.py's gift_wallet_xp()).
--
-- Required by:
--   database.py -> get_xp_wallet() / add_wallet_xp() / gift_wallet_xp()
--   discord_bot/cogs/_views_leveling_wallet.py -> Buy/Wallet/Gift buttons
--   payments_manual.py -> UNLOCK_HANDLERS["xp_wallet_*"]
--   config.py -> XP_WALLET_TIERS / XP_WALLET_EXPIRY_DAYS / gift limits
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_xp_wallet (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    clone_id INTEGER,
    balance BIGINT NOT NULL DEFAULT 0,
    -- Unspent balance is forfeited if not spent/gifted within
    -- config.XP_WALLET_EXPIRY_DAYS of the LAST top-up (refreshed on every
    -- purchase) — same "don't let this float grow forever" logic as
    -- discord_xp_boosts.expires_at, just applied to a balance instead of a
    -- multiplier. Checked lazily on read (get_xp_wallet), not a cron job.
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS discord_xp_wallet_guild_clone_user_key
    ON discord_xp_wallet (guild_id, COALESCE(clone_id, -1), user_id);

-- Audit trail for every purchase/gift-sent/gift-received — the actual
-- "check their bought boost xp" history a wallet needs once real money is
-- involved. Not read on any hot path, so no special indexing beyond the
-- lookup index below.
CREATE TABLE IF NOT EXISTS discord_xp_wallet_ledger (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    user_id BIGINT NOT NULL,
    delta BIGINT NOT NULL,          -- positive = credited, negative = debited
    reason TEXT NOT NULL,           -- 'purchase' | 'gift_sent' | 'gift_received'
    counterparty_id BIGINT,         -- the other party for gift_sent/gift_received, else NULL
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discord_xp_wallet_ledger_user
    ON discord_xp_wallet_ledger (guild_id, COALESCE(clone_id, -1), user_id, created_at DESC);

-- Server-wide temporary XP multiplier (the "boost whole server" option) —
-- same multiplier+expires_at shape as discord_xp_boosts (013), just keyed
-- per (guild, clone) with no user_id since it applies to every member.
CREATE TABLE IF NOT EXISTS discord_guild_xp_boosts (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    multiplier NUMERIC NOT NULL DEFAULT 2.0,
    expires_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS discord_guild_xp_boosts_guild_clone_key
    ON discord_guild_xp_boosts (guild_id, COALESCE(clone_id, -1));

-- ============================================================================
-- Migration Complete — auto-applied on bot startup (see database.py's
-- SCHEMA_VERSION bump-or-it-never-runs rule).
-- ============================================================================
-- Still manual (only if a product needs to change later):
-- 1. config.SELAR_PRODUCT_LINKS already has the real, live Selar product
--    URLs for all 5 keys (xp_wallet_small/medium/large/mega,
--    xp_server_boost) as of this migration.
-- 2. Each product's "Redirect URL after purchase" must point to
--      https://dash-production-c237.up.railway.app/unlock?payment_type=<key>
--    with the same Custom Checkout Form fields ("Discord username",
--    "Discord server name") as every other manual-payment product.
-- ============================================================================
