-- Phase 7 Primole: a second play currency in the shared cat wallet (account-bound, never traded, never turned back).
-- Additive and idempotent.
ALTER TABLE cat_wallets ADD COLUMN IF NOT EXISTS primole BIGINT NOT NULL DEFAULT 0;
