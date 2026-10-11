-- Cat storage-channel backfill: which OLD cats (adopted / pictured before the event log existed) have already been
-- written up, so a restart or a second tick never logs the same one twice. Additive, idempotent.
--   kind    'adoption' (ref_id = cat_adoptions.id) | 'picture' (ref_id = cat_image_pool.id)
--   status  'done' | 'skipped' (nothing to do or impossible, never retried) | 'retry' (failed, tried again later)
CREATE TABLE IF NOT EXISTS cat_log_backfill (
    kind        TEXT        NOT NULL,
    ref_id      BIGINT      NOT NULL,
    status      TEXT        NOT NULL,
    attempts    INTEGER     NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, ref_id)
);
