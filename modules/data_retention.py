"""Timed clean-up so the database doesn't keep more than the privacy page says.

Run by discord_bot/cogs/data_retention.py (bot side) and api/cron_expire_monetization.py (backup). Every purge is idempotent."""
import logging

from database import get_pool

logger = logging.getLogger(__name__)

FLOOD_MINUTES = 10        # spam checks look back seconds, so a few minutes is plenty
AI_LOG_DAYS = 90
SCAM_HIT_DAYS = 180       # matches modules/scam_reputation.LOOKBACK_DAYS
IP_HASH_DAYS = 30


BATCH = 2000              # rows per statement: short statements, so nothing waits long on a lock or hits a timeout
MAX_BATCHES = 25          # per purge run (so one run never takes long); the next run carries on where it stopped

# Each statement handles at most $1 rows and skips rows another process is already working on (FOR UPDATE SKIP LOCKED),
# so several bots can run it at the same time without waiting for each other.
SCRUB_FLOOD_TEXT = ("UPDATE flood_events SET message_text = NULL WHERE id IN (SELECT id FROM flood_events "
                    "WHERE message_text IS NOT NULL AND message_text NOT LIKE 'h1:%' LIMIT $1 FOR UPDATE SKIP LOCKED)")
SCRUB_SCAM_SNIPPETS = ("UPDATE scam_shield_hits SET snippet = NULL WHERE id IN (SELECT id FROM scam_shield_hits "
                       "WHERE snippet IS NOT NULL LIMIT $1 FOR UPDATE SKIP LOCKED)")
SCRUB_AI_ARGS = ("UPDATE ai_command_log SET args_json = NULL WHERE id IN (SELECT id FROM ai_command_log "
                 "WHERE args_json IS NOT NULL LIMIT $1 FOR UPDATE SKIP LOCKED)")


def _flood_delete_sql() -> str:
    return ("DELETE FROM flood_events WHERE id IN (SELECT id FROM flood_events "
            f"WHERE occurred_at < NOW() - INTERVAL '{FLOOD_MINUTES} minutes' LIMIT $1 FOR UPDATE SKIP LOCKED)")


async def _batched(conn, sql: str, *, batch: int = BATCH, max_batches: int = MAX_BATCHES) -> int:
    """Run `sql` (which takes the batch size as $1) until it changes nothing or max_batches is reached. Never raises."""
    total = 0
    try:
        for _ in range(max_batches):
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '15s'")      # one slow batch can't hang the bot
                n = int((await conn.execute(sql, batch)).split()[-1])
            total += n
            if n < batch:
                break
    except Exception:
        logger.exception("[retention] batched purge failed: %s", sql[:60])
    return total


async def scrub_stored_text() -> dict:
    """Blank text we no longer keep (old flood messages, scam snippets, AI command arguments), a little at a time."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return {"flood_text": await _batched(conn, SCRUB_FLOOD_TEXT),
                "scam_snippets": await _batched(conn, SCRUB_SCAM_SNIPPETS),
                "ai_args": await _batched(conn, SCRUB_AI_ARGS)}


async def _run(conn, sql: str) -> int:
    try:
        return int((await conn.execute(sql)).split()[-1])
    except Exception:
        logger.exception("[retention] purge failed: %s", sql[:60])
        return 0


async def purge_flood() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _batched(conn, _flood_delete_sql())


async def purge_daily() -> dict:
    pool = await get_pool()
    out = {}
    async with pool.acquire() as conn:
        out["flood"] = await _batched(conn, _flood_delete_sql())
        out["ai_log"] = await _batched(conn, "DELETE FROM ai_command_log WHERE id IN (SELECT id FROM ai_command_log "
                                              f"WHERE created_at < NOW() - INTERVAL '{AI_LOG_DAYS} days' LIMIT $1 FOR UPDATE SKIP LOCKED)")
        out["scam_hits"] = await _batched(conn, "DELETE FROM scam_shield_hits WHERE id IN (SELECT id FROM scam_shield_hits "
                                                f"WHERE created_at < NOW() - INTERVAL '{SCAM_HIT_DAYS} days' LIMIT $1 FOR UPDATE SKIP LOCKED)")
        out["click_ips"] = await _run(conn, f"UPDATE listing_click_log SET ip_hash = NULL, user_agent_hash = NULL WHERE (ip_hash IS NOT NULL OR user_agent_hash IS NOT NULL) AND clicked_at < NOW() - INTERVAL '{IP_HASH_DAYS} days'")
        out["browse_ips"] = await _run(conn, f"UPDATE category_browse_log SET ip_hash = NULL WHERE ip_hash IS NOT NULL AND browsed_at < NOW() - INTERVAL '{IP_HASH_DAYS} days'")
    out.update(await scrub_stored_text())
    return out
