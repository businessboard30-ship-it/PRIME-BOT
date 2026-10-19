-- Ops reports (bot health notes): every bot (main or clone) writes a row; the MAIN bot posts them to a channel in the
-- support server. Additive and idempotent.
CREATE TABLE IF NOT EXISTS ops_reports (
    id         BIGSERIAL PRIMARY KEY,
    bot_label  TEXT NOT NULL,
    key        TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    posted_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ops_reports_unposted_idx ON ops_reports (id) WHERE posted_at IS NULL;
