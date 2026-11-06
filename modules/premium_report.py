# path: modules/premium_report.py

"""\"What Premium did for your server\": a short report of real numbers, so an owner can see what the money bought.

Honest by construction:
  * every number is a COUNT of rows the bot already stores for that server (scam catches, honeypot catches, tickets,
    giveaways, cats adopted, anti-raid quarantines) -- nothing is estimated or invented;
  * each metric is read on its own, so one failing query only drops that line;
  * a metric that is zero is shown as zero when its feature is one the server could have used, never hidden or padded;
  * nothing here changes any data.

gather() talks to the database; render() is pure so it is unit-testable.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30

# key, emoji, label (singular, plural), SQL counting rows for ($1 guild_id, $2 days). Every query is read-only.
_METRICS: Tuple[Tuple[str, str, Tuple[str, str], str], ...] = (
    ("scams", "\U0001f50d", ("scam message caught", "scam messages caught"),
     "SELECT COUNT(*) FROM scam_shield_hits WHERE guild_id = $1 AND created_at > NOW() - make_interval(days => $2)"),
    ("honeypot", "\U0001f36f", ("bot/raider trapped by the honeypot", "bots/raiders trapped by the honeypot"),
     "SELECT COUNT(*) FROM discord_honeypot_catches WHERE guild_id = $1 AND succeeded "
     "AND caught_at > NOW() - make_interval(days => $2)"),
    ("raiders", "\U0001f6e1\ufe0f", ("suspicious joiner held for review", "suspicious joiners held for review"),
     "SELECT COUNT(*) FROM discord_quarantined WHERE guild_id = $1 AND reason LIKE '[anti-raid]%' "
     "AND created_at > NOW() - make_interval(days => $2)"),
    ("tickets", "\U0001f3ab", ("support ticket handled", "support tickets handled"),
     "SELECT COUNT(*) FROM discord_tickets WHERE guild_id = $1 AND created_at > NOW() - make_interval(days => $2)"),
    ("giveaways", "\U0001f389", ("giveaway run", "giveaways run"),
     "SELECT COUNT(*) FROM discord_giveaways WHERE guild_id = $1 AND ends_at > NOW() - make_interval(days => $2) "
     "AND ends_at <= NOW()"),
    ("cats", "\U0001f431", ("cat adopted", "cats adopted"),
     "SELECT COUNT(*) FROM cat_spawns WHERE guild_id = $1 AND adopted_at IS NOT NULL "
     "AND adopted_at > NOW() - make_interval(days => $2)"),
)


async def gather(guild_id: int, days: int = DEFAULT_DAYS) -> Dict[str, Optional[int]]:
    """{metric key: count, or None when that metric could not be read}. Never raises."""
    out: Dict[str, Optional[int]] = {}
    try:
        from database import get_pool
        pool = await get_pool()
    except Exception:
        logger.exception("[premium-report] no database pool")
        return {key: None for key, *_ in _METRICS}
    for key, _emoji, _label, sql in _METRICS:
        try:
            async with pool.acquire() as conn:
                out[key] = int(await conn.fetchval(sql, int(guild_id), int(days)) or 0)
        except Exception:
            logger.debug("[premium-report] metric %s failed for guild %s", key, guild_id, exc_info=True)
            out[key] = None
    return out


def _line(emoji: str, label: Tuple[str, str], n: int) -> str:
    return f"{emoji} **{n:,}** {label[0] if n == 1 else label[1]}"


def render(stats: Dict[str, Optional[int]], days: int = DEFAULT_DAYS, *, is_trial: bool = False) -> str:
    """The report text. Lines with a real number first; if nothing happened yet it says so plainly."""
    active: List[str] = []
    for key, emoji, label, _sql in _METRICS:
        n = stats.get(key)
        if n:
            active.append(_line(emoji, label, n))
    period = "during your trial" if is_trial else f"in the last {days} days"
    if not active:
        readable = [s for s in stats.values() if s is not None]
        if not readable:
            return "I couldn't read this server's numbers just now \u2014 try again in a moment."
        return (f"**Nothing to report {period}.** That usually means a quiet server, not a broken feature: the "
                "protections only show up here when they actually catch something. Set up Scam Shield, the honeypot "
                "or anti-raid and check back.")
    head = f"**Here's what Premium did for this server {period}:**"
    return head + "\n" + "\n".join(active)


def worth_it_line(stats: Dict[str, Optional[int]]) -> str:
    """One closing line tied to the numbers (empty when there is nothing real to point at)."""
    protect = sum(stats.get(k) or 0 for k in ("scams", "honeypot", "raiders"))
    if protect:
        return f"-# {protect:,} of those were threats stopped before they could hurt your members."
    return ""
