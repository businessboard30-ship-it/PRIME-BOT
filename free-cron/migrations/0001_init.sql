CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  github_id INTEGER NOT NULL UNIQUE,
  login TEXT NOT NULL,
  premium_until INTEGER NOT NULL DEFAULT 0,         -- ms since epoch; written ONLY by the admin endpoint (and later a payment webhook)
  create_window_start INTEGER NOT NULL DEFAULT 0,
  create_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'GET',
  body TEXT,
  every_minutes INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at INTEGER NOT NULL,
  last_run_at INTEGER,
  last_status INTEGER,                              -- HTTP status, or 0 for a timeout / network error. The response body is never stored.
  last_ms INTEGER,
  fail_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS jobs_account_idx ON jobs (account_id);
