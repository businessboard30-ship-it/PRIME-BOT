-- Public status pages. A page lists chosen crons by an owner-chosen label; the public view never exposes URLs, methods, bodies or status text.
-- blocked = disabled by the service owner (abuse); the page owner cannot undo it. Keep identical to ensureStatusTables() in src/index.js.
CREATE TABLE IF NOT EXISTS status_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  blocked INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS status_pages_acct_idx ON status_pages (account_id);
CREATE TABLE IF NOT EXISTS status_page_jobs (
  page_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  PRIMARY KEY (page_id, job_id)
);
CREATE INDEX IF NOT EXISTS status_page_jobs_job_idx ON status_page_jobs (job_id);
