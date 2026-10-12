"""Timed clean-up so the database doesn't keep more than the privacy page says.

Run by discord_bot/cogs/data_retention.py (bot side) and api/cron_expire_monetization.py (backup). Every purge is idempotent."""
import logging

from database import get_pool

logger = logging.getLogger(__name__)

FLOOD_MINUTES = 10        # spam checks look back seconds, so a few minutes is plenty
AI_LOG_DAYS = 90
SCAM_HIT_DAYS = 180       # matches modules/scam_reputation.LOOKBACK_DAYS
IP_HASH_DAYS = 30


async def _run(conn, sql: str) -> int:
    try:
        return int((await conn.execute(sql)).split()[-1])
    except Exception:
        logger.exception("[retention] purge failed: %s", sql[:60])
        return 0


async def purge_flood() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _run(conn, f"DELETE FROM flood_events WHERE occurred_at < NOW() - INTERVAL '{FLOOD_MINUTES} minutes'")


async def purge_daily() -> dict:
    pool = await get_pool()
    out = {}
    async with pool.acquire() as conn:
        out["flood"] = await _run(conn, f"DELETE FROM flood_events WHERE occurred_at < NOW() - INTERVAL '{FLOOD_MINUTES} minutes'")
        out["ai_log"] = await _run(conn, f"DELETE FROM ai_command_log WHERE created_at < NOW() - INTERVAL '{AI_LOG_DAYS} days'")
        out["scam_hits"] = await _run(conn, f"DELETE FROM scam_shield_hits WHERE created_at < NOW() - INTERVAL '{SCAM_HIT_DAYS} days'")
        out["click_ips"] = await _run(conn, f"UPDATE listing_click_log SET ip_hash = NULL, user_agent_hash = NULL WHERE (ip_hash IS NOT NULL OR user_agent_hash IS NOT NULL) AND clicked_at < NOW() - INTERVAL '{IP_HASH_DAYS} days'")
        out["browse_ips"] = await _run(conn, f"UPDATE category_browse_log SET ip_hash = NULL WHERE ip_hash IS NOT NULL AND browsed_at < NOW() - INTERVAL '{IP_HASH_DAYS} days'")
    return out
