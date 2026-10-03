-- Public post + description for the owner-panel referral giveaway.
-- Additive and idempotent like 001-027.
ALTER TABLE referral_giveaways ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE referral_giveaways ADD COLUMN IF NOT EXISTS channel_id  BIGINT;   -- where the public post lives
ALTER TABLE referral_giveaways ADD COLUMN IF NOT EXISTS message_id  BIGINT;   -- the public post itself
CREATE INDEX IF NOT EXISTS idx_referral_giveaways_message ON referral_giveaways (message_id);
