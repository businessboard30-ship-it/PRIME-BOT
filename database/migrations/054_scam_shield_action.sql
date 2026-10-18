-- Scam Shield: what happens to the person who posted a caught scam (per server). Additive, idempotent.
-- 'delete' (default, the old behaviour: only remove the message) | 'timeout' | 'quarantine' | 'kick' | 'ban'
ALTER TABLE discord_scam_shield_guild ADD COLUMN IF NOT EXISTS punish_action TEXT NOT NULL DEFAULT 'delete';
