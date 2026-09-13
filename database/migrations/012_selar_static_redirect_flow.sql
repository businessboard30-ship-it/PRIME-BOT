-- ============================================================================
-- Migration 012: Selar static-redirect web-confirmation flow
-- ============================================================================
-- Replaces the old per-purchase signed redirect_url design (migration 010 /
-- utils/selar_signing.py) — Selar's product-level "redirect after purchase"
-- turns out to be a single static URL per product with nothing appended, so
-- there is no dynamic reference/sig/buyer_id to verify on the way back.
--
-- New shape: the buyer signs in with Discord on the static /unlock page
-- (reusing api/discord_login_oauth.py), and the backend matches their OAuth-
-- verified user_id against the most recent 'pending' Selar payment_logs row
-- for that (user_id, payment_type) pair — the row start_manual_payment()
-- already wrote the moment the "Pay on Selar" button was tapped in Discord.
--
-- Required by:
--   database.py -> log_payment() / get_latest_pending_selar_payment()
--   api/discord_login_oauth.py -> create_login_oauth_state()/pop_login_oauth_state()
--   api/selar_submit.py
-- ============================================================================

-- clone_id was previously only ever carried through the signed redirect_url
-- query string (never persisted) — account-level purchases (discord_clone,
-- discord_clone_monetization) need it to resolve the right approver/bot
-- token, and now that there's no signed URL round-tripping it back to us,
-- it has to live on the row itself.
ALTER TABLE payment_logs
    ADD COLUMN IF NOT EXISTS clone_id INTEGER;

-- provider/group_id: log_payment()/claim_manual_payment_for_review() have
-- referenced these for a while, but the migration that was meant to add
-- them (010_selar_manual_payment_web_flow.sql) lived at the repo root
-- instead of database/migrations/ and was therefore NEVER actually
-- executed by the startup migration runner below — folded in here so this
-- migration alone brings a fresh/existing DB fully up to date in one go.
ALTER TABLE payment_logs
    ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'paystack';

ALTER TABLE payment_logs
    ADD COLUMN IF NOT EXISTS group_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_payment_logs_reference_status_provider
    ON payment_logs (paystack_reference, status, provider);

CREATE INDEX IF NOT EXISTS idx_payment_logs_provider
    ON payment_logs (provider);

-- get_latest_pending_selar_payment() looks up the newest 'pending' row for
-- (user_id, payment_type, provider) — index the triple so that lookup
-- doesn't do a sequential scan under load.
CREATE INDEX IF NOT EXISTS idx_payment_logs_user_type_provider_status
    ON payment_logs (user_id, payment_type, provider, status, payment_id DESC);

-- "Sign in with Discord" can now be entered from more than one place
-- (the landing page's /login/servers AND the /unlock confirmation page) —
-- return_to remembers which one asked, so the OAuth callback knows where to
-- send the browser back to. NULL/absent means the old default
-- (/login/servers), so existing sign-in links keep working unchanged.
ALTER TABLE discord_login_oauth_states
    ADD COLUMN IF NOT EXISTS return_to VARCHAR(500);

-- ============================================================================
-- Migration Complete — this file is auto-applied on bot startup by
-- database.py's init routine (same pattern as 001-010 in this folder).
-- ============================================================================
-- Still manual (not something SQL can do for you):
-- 1. On each Selar product, set "Redirect URL after purchase" (a static,
--    per-product field — confirmed no query params are appended) to:
--      https://dash-production-c237.up.railway.app/unlock?payment_type=<type>
--    e.g. .../unlock?payment_type=ultra_welcome_pack
-- 2. On each Selar product, add two REQUIRED Custom Checkout Form fields:
--    "Discord username" and "Discord server name" — kept as the human
--    cross-check an admin uses against the Selar dashboard before approving,
--    now that the web flow's own matching is OAuth-based instead of those
--    fields being machine-read.
-- 3. utils/selar_signing.py, api/selar_redirect.py and the old
--    reference/sig-based verification are no longer part of the live path —
--    left in place for now rather than deleted outright; safe to remove in a
--    follow-up once the new flow has been running cleanly.
