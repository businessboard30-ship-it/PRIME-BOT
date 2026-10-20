-- path: database/migrations/076_anime_plus.sql

-- Anime feed D5: the PERSONAL upgrade. A separate entitlement for one person: it never makes a server premium and never touches the
-- guild premium tables. (A premium SERVER unlocks the same features for everyone in it; that check is code, not data.)
-- Additive and idempotent.

-- One row per (person, bot): active until expires_at. Renewing extends from the later of now and the current expiry.
CREATE TABLE IF NOT EXISTS anime_plus_entitlements (
    user_id           BIGINT NOT NULL,
    clone_id          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active',
    payment_reference TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, clone_id)
);

-- One row per checkout, same shape as discord_image_search_unlock_payments. Created 'pending' at checkout and completed by the
-- payment webhook (server to server), so a restart between paying and tapping Verify never loses a paid upgrade.
CREATE TABLE IF NOT EXISTS anime_plus_payments (
    id                SERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL,
    clone_id          INTEGER NOT NULL DEFAULT 0,
    provider          TEXT NOT NULL DEFAULT 'paystack',
    payment_reference TEXT UNIQUE NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    days              INTEGER NOT NULL DEFAULT 30,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS anime_plus_payments_user_idx ON anime_plus_payments (user_id, id DESC);
