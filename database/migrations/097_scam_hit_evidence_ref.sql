-- Scam Shield: remember WHERE the archived copy of a caught scam lives (pointer only, no message text is stored here).
-- Lets the AI scam lookup show the scam link and image from that copy. Additive / idempotent.
ALTER TABLE scam_shield_hits ADD COLUMN IF NOT EXISTS evidence_channel_id BIGINT;
ALTER TABLE scam_shield_hits ADD COLUMN IF NOT EXISTS evidence_message_id BIGINT;
