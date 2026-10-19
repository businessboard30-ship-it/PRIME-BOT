-- Market voice messages (R2a): a buyer sends one short voice message to the seller of an OPEN listing, relayed by the bot.
-- Audio bytes live here ONLY while a message is pending; every other status must have audio NULL (enforced by the CHECK).
-- Additive and idempotent. Play coins only.
CREATE TABLE IF NOT EXISTS market_voice (
    id            BIGSERIAL PRIMARY KEY,
    listing_id    BIGINT NOT NULL,
    sender_id     BIGINT NOT NULL,
    receiver_id   BIGINT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',        -- pending | opened | expired | deleted
    duration_ms   INT NOT NULL CHECK (duration_ms > 0),
    size_bytes    INT NOT NULL CHECK (size_bytes > 0),
    audio         BYTEA,
    card_channel  BIGINT,                                 -- the seller's card (Play / Report / Block)
    card_message  BIGINT,
    play_channel  BIGINT,                                 -- the message the audio was played in (deleted after a short time)
    play_message  BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    opened_at     TIMESTAMPTZ,
    delete_after  TIMESTAMPTZ,
    closed_at     TIMESTAMPTZ,
    CHECK (status = 'pending' OR audio IS NULL),
    CHECK (audio IS NULL OR octet_length(audio) <= 400000),
    CHECK (sender_id <> receiver_id)
);
CREATE INDEX IF NOT EXISTS market_voice_receiver_idx ON market_voice (receiver_id, status);
CREATE INDEX IF NOT EXISTS market_voice_sender_idx   ON market_voice (sender_id, created_at);
CREATE INDEX IF NOT EXISTS market_voice_listing_idx  ON market_voice (listing_id, sender_id);
CREATE INDEX IF NOT EXISTS market_voice_due_idx      ON market_voice (status, expires_at);

-- Sellers choose whether buyers may send them voice messages. Off unless they turn it on.
CREATE TABLE IF NOT EXISTS market_voice_settings (
    owner_id   BIGINT PRIMARY KEY,
    voice_on   BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reports keep metadata only (never audio).
CREATE TABLE IF NOT EXISTS market_reports (
    id          BIGSERIAL PRIMARY KEY,
    voice_id    BIGINT NOT NULL,
    reporter_id BIGINT NOT NULL,
    reported_id BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (voice_id, reporter_id)
);
CREATE INDEX IF NOT EXISTS market_reports_reported_idx ON market_reports (reported_id, created_at);
