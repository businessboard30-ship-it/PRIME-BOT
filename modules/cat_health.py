# path: modules/cat_health.py
"""Cat picture health report: counts where every cat picture came from and why new ones did NOT arrive, then DMs the
bot owners a short plain-language report (so nobody has to read logs).

- record(event) is a cheap in-memory counter (never raises, never blocks).
- report_text() turns the counters into a short message with a verdict and the likely fix.
- send_report(bot) DMs it via image_host.notify_owners (cooldown applies). The cat cog calls it every few hours and
  also right after a clear failure (alert()).
Counters reset after each report, so every report covers only the period since the last one.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

REPORT_EVERY_HOURS = 6
STALE_REPORT_COOLDOWN = 6 * 3600
ALERT_COOLDOWN = 3 * 3600

_counts: Counter = Counter()
_since = time.time()
_last_error: Optional[str] = None

LABELS = {
    "pool_manual": "shown from stored pool (/cat or Cat button)",
    "pool_unseen": "shown from stored pool (automatic spawn)",
    "pool_repeat": "repeat from pool (API gave nothing)",
    "api_call": "cat API calls made",
    "api_ok": "cat API calls that returned pictures",
    "api_http_error": "cat API calls that failed (HTTP error)",
    "api_exception": "cat API calls that crashed or timed out",
    "api_empty": "cat API calls that returned no usable pictures",
    "cand_known": "API pictures skipped: already in pool",
    "cand_download": "API pictures skipped: download failed",
    "cand_quality": "API pictures skipped: failed quality filter",
    "cand_vision": "API pictures skipped: AI said not a nice cat photo",
    "store_ok": "NEW pictures saved to pool",
    "store_fail": "pictures NOT saved (duplicate or hosting channel problem)",
    "direct_used": "API picture used directly (could not be pooled)",
    "dup_blocked": "duplicate spawns BLOCKED (same picture already open in that server)",
    "recent_fallback": "old picture reused because nothing else worked",
}


def record(event: str, n: int = 1, error: Optional[str] = None) -> None:
    global _last_error
    try:
        _counts[event] += n
        if error:
            _last_error = str(error)[:200]
    except Exception:
        pass


def snapshot() -> dict:
    return dict(_counts)


def verdict(c: dict, pool_size: Optional[int], has_key: bool, has_vision_key: bool) -> list:
    """Plain-language findings, most important first."""
    out = []
    shown_pool = c.get("pool_manual", 0) + c.get("pool_unseen", 0) + c.get("pool_repeat", 0)
    if c.get("api_call", 0) == 0 and shown_pool:
        out.append("The cat API was NOT called at all. Everything shown came from the stored pool. "
                   "That is by design once the pool has 20+ pictures: /cat and the Cat button never call the API.")
    if c.get("api_http_error", 0) or c.get("api_exception", 0):
        out.append("The cat API is failing. Check CAT_API_KEY (invalid or rate-limited) and Railway outbound network.")
    if c.get("api_ok", 0) and not c.get("store_ok", 0):
        if c.get("cand_quality", 0) >= c.get("api_ok", 0):
            out.append("API pictures arrive but every one fails the quality filter, so none are added.")
        if c.get("cand_vision", 0):
            out.append("The AI check is rejecting pictures. Raise CAT_VISION_DAILY or unset GEMINI_API_KEY to test.")
        if c.get("cand_known", 0):
            out.append("API keeps returning pictures the pool already holds (duplicates).")
        if c.get("store_fail", 0):
            out.append("Pictures pass the filters but cannot be saved. The image-hosting channel is missing, full or "
                       "the bot lacks send/attach permission there.")
    if not has_key:
        out.append("CAT_API_KEY is not set on this bot: The Cat API limits keyless use very hard.")
    if c.get("recent_fallback", 0):
        out.append("Some cats were old pictures reused from memory because both pool and API failed.")
    if not out:
        out.append("No problem found: new pictures are arriving normally.")
    return out


def report_text(pool_size: Optional[int], has_key: bool, has_vision_key: bool, hours: float) -> str:
    c = snapshot()
    lines = [f"🐱 Cat picture report (last {hours:.1f}h)"]
    if pool_size is not None:
        lines.append(f"Pool size: {pool_size}")
    for k, label in LABELS.items():
        if c.get(k):
            lines.append(f"• {label}: {c[k]}")
    if not c:
        lines.append("• no cat pictures were requested in this period")
    lines.append("")
    lines.append("Verdict:")
    lines += [f"- {v}" for v in verdict(c, pool_size, has_key, has_vision_key)]
    if _last_error:
        lines.append(f"Last error: {_last_error}")
    return "\n".join(lines)[:1800]


def reset() -> None:
    global _since, _last_error
    _counts.clear()
    _since = time.time()
    _last_error = None


async def send_report(bot, force: bool = False) -> bool:
    """DM the owners the report and reset the counters. Never raises."""
    try:
        import config
        from modules import cat_pool, image_host
        try:
            size = await cat_pool.pool_count()
        except Exception:
            size = None
        hours = max(0.01, (time.time() - _since) / 3600)
        from modules import cat_vision
        text = report_text(size, bool(getattr(config, "CAT_API_KEY", "")), bool(cat_vision.api_key()), hours)
        sent = await image_host.notify_owners(bot, "cat-health", text, cooldown=0 if force else STALE_REPORT_COOLDOWN)
        if sent:
            reset()
        return sent
    except Exception:
        logger.warning("[cat-health] report failed", exc_info=True)
        return False


async def alert(bot, reason: str) -> None:
    """Immediate short DM for a clear failure (rate limited per reason)."""
    try:
        from modules import image_host
        await image_host.notify_owners(bot, f"cat-alert:{reason}", f"🐱 Cat pictures problem: {reason}",
                                       cooldown=ALERT_COOLDOWN)
    except Exception:
        logger.debug("[cat-health] alert failed", exc_info=True)
