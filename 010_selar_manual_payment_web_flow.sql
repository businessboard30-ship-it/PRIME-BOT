-- ============================================================================
-- Migration 010: Selar manual-payment web-confirmation flow
-- ============================================================================
-- Required by:
--   database.py -> claim_manual_payment_for_review() / log_payment()
--   payments_manual.py -> start_manual_payment() (calls log_payment(..., provider=PROVIDER))
--   api/selar_redirect.py, api/selar_submit.py, api/selar_status.py
--
-- schema.sql's payment_logs definition does not declare `provider` or
-- `group_id`, but log_payment() already inserts into both columns for
-- every caller (not just Selar) -- this migration is defensive (IF NOT
-- EXISTS) in case a prior manual ALTER TABLE already added them directly
-- in production without a matching migration file ever being committed.
-- Running this against a DB that already has these columns is a safe no-op.
-- ============================================================================

ALTER TABLE payment_logs
    ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'paystack';

ALTER TABLE payment_logs
    ADD COLUMN IF NOT EXISTS group_id INTEGER;

-- claim_manual_payment_for_review() filters on
-- (paystack_reference, status, provider) together -- index the pair so the
-- atomic UPDATE ... WHERE ... RETURNING doesn't do a sequential scan under
-- concurrent double-tap/reload traffic on the /unlock page.
CREATE INDEX IF NOT EXISTS idx_payment_logs_reference_status_provider
    ON payment_logs (paystack_reference, status, provider);

CREATE INDEX IF NOT EXISTS idx_payment_logs_provider
    ON payment_logs (provider);

-- status is VARCHAR(50) with no CHECK constraint today, so 'awaiting_review'
-- and 'rejected' (the two new values the Selar web flow introduces, on top
-- of the existing 'pending'/'completed') already fit without a migration --
-- documented here for anyone reading schema history, not enforced by SQL.
-- Valid payment_logs.status values as of this migration:
--   pending          -- initial row from log_payment()
--   awaiting_review  -- buyer tapped "I've Paid" on /unlock; claim_manual_payment_for_review() set this
--   completed        -- admin approved (mark_payment_paid path)
--   rejected         -- admin rejected (mark_manual_payment_rejected)

-- ============================================================================
-- Migration Complete
-- ============================================================================
-- Next steps:
-- 1. Run this against your actual Postgres instance (Railway/Supabase) before
--    deploying database.py's claim_manual_payment_for_review() / log_payment()
--    changes, or every call referencing `provider` will fail with
--    "column provider does not exist" if it isn't already present in prod.
-- 2. Confirm in your DB console whether `provider`/`group_id` already exist
--    (schema drift is likely, since log_payment() has referenced them for a
--    while) -- if so this migration is a harmless no-op due to IF NOT EXISTS.
-- 3. Update sql/schema.sql itself to declare `provider` and `group_id` on
--    payment_logs, so schema.sql stops silently lying about the real table shape.
