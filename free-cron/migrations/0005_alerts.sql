-- Failure alerts (Discord webhook). Keep identical to ensureAlertTables() in src/index.js.
-- alerting = 1 while a "down" alert has been sent and the cron has not recovered yet (dedupe). last_alert_at = when the last alert was queued.
ALTER TABLE jobs ADD COLUMN alerting INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN last_alert_at INTEGER;
-- The webhook URL is a credential: never returned in full by the API, never logged.
CREATE TABLE IF NOT EXISTS account_settings (
  account_id INTEGER PRIMARY KEY,
  discord_webhook TEXT,
  alerts_enabled INTEGER NOT NULL DEFAULT 1,
  alert_window_start INTEGER NOT NULL DEFAULT 0,
  alert_count INTEGER NOT NULL DEFAULT 0,
  last_test_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
-- Small queue so a slow or failing webhook can never delay or break runDue(). Rows are removed once sent, or after 3 failed tries or 1 day.
CREATE TABLE IF NOT EXISTS alert_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  job_name TEXT NOT NULL,
  detail TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_outbox_acct_idx ON alert_outbox (account_id);
