-- "Run now" budget: at most 30 manual runs per account per hour. Keep identical to ensureManualColumns() in src/index.js.
ALTER TABLE accounts ADD COLUMN manual_window_start INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN manual_count INTEGER NOT NULL DEFAULT 0;
