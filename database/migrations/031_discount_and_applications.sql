-- Yearly Premium 50% first-payment discount codes + Application Forms. Additive, idempotent.

-- One code per person, ever. `user_id` binds the code to its owner; `reserved_reference` ties it
-- to the one checkout (payment_logs.paystack_reference) allowed to use the discount.
CREATE TABLE IF NOT EXISTS premium_discount_codes (
    id                  BIGSERIAL PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    user_id             BIGINT NOT NULL,
    payment_type        TEXT NOT NULL DEFAULT 'premium_yearly',
    percent_off         INTEGER NOT NULL DEFAULT 50,
    status              TEXT NOT NULL DEFAULT 'issued',   -- issued | reserved | redeemed | revoked
    gumroad_offer_id    TEXT,
    reserved_reference  TEXT,
    guild_id            BIGINT,
    redeemed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS premium_discount_codes_user_type_key
    ON premium_discount_codes (user_id, payment_type);
CREATE INDEX IF NOT EXISTS premium_discount_codes_ref_idx
    ON premium_discount_codes (reserved_reference);

CREATE TABLE IF NOT EXISTS discord_application_forms (
    id                 BIGSERIAL PRIMARY KEY,
    guild_id           BIGINT NOT NULL,
    clone_id           INTEGER,
    creator_id         BIGINT NOT NULL,
    title              TEXT NOT NULL DEFAULT '📝 Apply here',
    description        TEXT NOT NULL DEFAULT 'Press the button below to apply.',
    button_label       TEXT NOT NULL DEFAULT 'Apply',
    button_emoji       TEXT,
    color_key          TEXT NOT NULL DEFAULT 'blurple',
    questions          JSONB NOT NULL DEFAULT '[]'::jsonb,
    post_channel_id    BIGINT,
    review_channel_id  BIGINT,
    accept_role_id     BIGINT,
    status             TEXT NOT NULL DEFAULT 'draft',   -- draft | active | closed
    panel_channel_id   BIGINT,
    panel_message_id   BIGINT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS discord_application_forms_guild_idx
    ON discord_application_forms (guild_id, status);

CREATE TABLE IF NOT EXISTS discord_application_submissions (
    id                 BIGSERIAL PRIMARY KEY,
    form_id            BIGINT NOT NULL REFERENCES discord_application_forms(id) ON DELETE CASCADE,
    guild_id           BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    answers            JSONB NOT NULL DEFAULT '[]'::jsonb,
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | declined
    review_channel_id  BIGINT,
    review_message_id  BIGINT,
    decided_by         BIGINT,
    decided_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- one pending application per person per form
CREATE UNIQUE INDEX IF NOT EXISTS discord_application_pending_key
    ON discord_application_submissions (form_id, user_id) WHERE status = 'pending';
