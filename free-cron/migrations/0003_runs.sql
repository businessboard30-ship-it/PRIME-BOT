-- One row per cron run: only status, duration and time. Response bodies and headers are never stored.
-- Kept 7 days for free accounts and 30 days for Premium (pruned in scheduled()). Keep identical to ensureRunsTable() in src/index.js.
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  ran_at INTEGER NOT NULL,
  status INTEGER NOT NULL,
  ms INTEGER NOT NULL,
  ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_job_idx ON runs (job_id, ran_at);
CREATE INDEX IF NOT EXISTS runs_acct_idx ON runs (account_id, ran_at);
CREATE INDEX IF NOT EXISTS runs_time_idx ON runs (ran_at);
