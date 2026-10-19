-- Primole (🔮): second play currency for the cat game, kept on the shared cat wallet (one per person for every bot).
-- Play currency only: nothing here is bought with, or sold for, real money. Additive and idempotent.
-- Account-bound: no table or function moves Primole between people, and it never converts back to coins or diamonds.
ALTER TABLE cat_wallets ADD COLUMN IF NOT EXISTS primole BIGINT NOT NULL DEFAULT 0;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cat_wallets_primole_nonneg') THEN
        ALTER TABLE cat_wallets ADD CONSTRAINT cat_wallets_primole_nonneg CHECK (primole >= 0);
    END IF;
END $$;
