-- Privacy clean-up. Idempotent.
-- 1. Message text is no longer kept: flood rows keep only a hash, scam hits keep no snippet.
UPDATE flood_events SET message_text = NULL WHERE message_text IS NOT NULL AND message_text NOT LIKE 'h1:%';
CREATE INDEX IF NOT EXISTS idx_flood_events_time ON flood_events (occurred_at);
DELETE FROM flood_events WHERE occurred_at < NOW() - INTERVAL '1 hour';
UPDATE scam_shield_hits SET snippet = NULL WHERE snippet IS NOT NULL;
-- 2. AI command arguments can contain what someone typed: keep only the command name and outcome.
UPDATE ai_command_log SET args_json = NULL WHERE args_json IS NOT NULL;
