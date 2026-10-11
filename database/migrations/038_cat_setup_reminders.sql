-- Cat game reminder for servers that have it switched off: at most 2 reminders, inside ONE week from the first one,
-- then never again (modules/cat_reminder.py). Additive, idempotent.
CREATE TABLE IF NOT EXISTS cat_setup_reminders (
    guild_id      BIGINT      NOT NULL,
    clone_id      BIGINT,
    sent_count    INT         NOT NULL DEFAULT 0,
    first_sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_sent_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_setup_reminders ON cat_setup_reminders (guild_id, COALESCE(clone_id, -1));
