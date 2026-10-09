-- Gumroad license keys redeemed for Premium. The key is kept because a membership is re-checked with Gumroad every day.
-- A key belongs to the first account that redeems it (key_hash is unique), so a leaked row can't be used by anyone else.
CREATE TABLE IF NOT EXISTS licenses (
  key_hash TEXT PRIMARY KEY,
  license_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  recurring INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  checked_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS licenses_due_idx ON licenses (active, recurring, checked_at);
CREATE INDEX IF NOT EXISTS licenses_account_idx ON licenses (account_id);
