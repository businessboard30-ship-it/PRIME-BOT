-- Owner-panel Batch 4 (money). Additive and idempotent like 001-022.

-- Webhook / reconciliation failures, written by modules/admin_money.record_failure()
-- from api/paystack_webhook.py and api/gumroad_webhook.py. Everything in
-- `detail` is passed through admin_ops.mask_secrets() before it is stored.
CREATE TABLE IF NOT EXISTS payment_failures (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,              -- 'paystack' | 'gumroad'
    kind          TEXT NOT NULL,              -- e.g. 'bad_signature', 'product mismatch', 'handler_error'
    reference     TEXT,
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dismissed_by  BIGINT,
    dismissed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_payment_failures_open
    ON payment_failures (created_at DESC) WHERE dismissed_at IS NULL;

-- Reversal bookkeeping. A reversed payment keeps its row; status becomes 'reversed'.
ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMPTZ;
ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS reversed_by BIGINT;

-- Discount codes. NOT yet read by checkout (management screen only).
CREATE TABLE IF NOT EXISTS discount_codes (
    code        TEXT PRIMARY KEY,
    percent_off INTEGER NOT NULL CHECK (percent_off BETWEEN 1 AND 100),
    max_uses    INTEGER CHECK (max_uses IS NULL OR max_uses > 0),
    uses        INTEGER NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
