-- Scam Shield: cross-server scam word/domain/image filter (modules/scam_shield.py). Additive, idempotent.
CREATE TABLE IF NOT EXISTS scam_shield_rules (
    id          SERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,                 -- 'word' | 'domain' | 'image'
    pattern     TEXT NOT NULL,                 -- word / host name / 16-hex-digit dHash
    note        TEXT,
    added_by    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, pattern)
);
CREATE TABLE IF NOT EXISTS scam_shield_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scam_shield_hits (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    clone_id    BIGINT,
    channel_id  BIGINT,
    user_id     BIGINT NOT NULL,
    kind        TEXT NOT NULL,
    matched     TEXT,
    rule_id     INTEGER,
    snippet     TEXT,
    deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scam_shield_hits_recent ON scam_shield_hits (id DESC);

-- Starter rules (the MrBeast / fatowin casino scam). Owner can remove or add more in /admin.
INSERT INTO scam_shield_rules (kind, pattern, note) VALUES
    ('word',   'fatowin',          'MrBeast crypto-casino scam'),
    ('domain', 'fatowin.com',      'MrBeast crypto-casino scam'),
    ('image',  '3734283e39391d31', 'scam screenshot: fake MrBeast post / withdrawal #1'),
    ('image',  '5ebe31a7a925a525', 'scam screenshot: fake MrBeast post / withdrawal #2'),
    ('image',  '1f2024a826232323', 'scam screenshot: fake MrBeast post / withdrawal #3'),
    ('image',  '2cb08ea3e363b070', 'scam screenshot: fake MrBeast post / withdrawal #4')
ON CONFLICT (kind, pattern) DO NOTHING;
