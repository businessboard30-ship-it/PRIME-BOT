-- Owner-panel referral giveaway. Additive and idempotent like 001-026.

-- When each referral code was redeemed. users.ad_referred_by (set once, forever)
-- has no timestamp, so a giveaway can only count redemptions recorded here.
-- Redemptions made before this table existed are not counted.
CREATE TABLE IF NOT EXISTS ad_referral_redemptions (
    referred_user_id BIGINT PRIMARY KEY,
    referrer_id      BIGINT NOT NULL,
    redeemed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ad_referral_redemptions_referrer
    ON ad_referral_redemptions (referrer_id, redeemed_at);

-- One row per referral giveaway. Winners are the top referrers inside
-- [starts_at, ends_at]. Prize is either handed out by hand ('manual') or is a
-- Discord role granted automatically ('role').
CREATE TABLE IF NOT EXISTS referral_giveaways (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    prize         TEXT NOT NULL,
    prize_kind    TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'role'
    guild_id      BIGINT,                           -- needed for 'role'
    role_id       BIGINT,                           -- needed for 'role'
    winner_count  INTEGER NOT NULL DEFAULT 1,
    min_referrals INTEGER NOT NULL DEFAULT 1,
    starts_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ends_at       TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'ended'
    winners_json  TEXT,                             -- [{"user_id":..,"count":..,"awarded":bool,"role_ok":bool|null}]
    created_by    BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_referral_giveaways_status ON referral_giveaways (status, ends_at);
