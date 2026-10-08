# path: modules/admin_snapshot.py
"""Worker -> web bridge for the owner dashboard.

The web service (api_server.py) is a different Railway process from the bot worker, so it cannot see
uptime, latency, the log ring buffer or the worker's config. The main bot publishes them here every
~60 s into bot_status_snapshots; the web only ever READS them. Everything is masked BEFORE it is written,
so a leaked row can't expose a secret. Pure builders + one publisher, no Discord objects kept.
"""
import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

KEY_MAIN, KEY_LOGS, KEY_CONFIG = "main", "logs", "config"
LOG_LINES = 200
LOG_MSG_MAX = 400
STALE_AFTER_S = 180          # web shows a warning when the newest snapshot is older than this
PUBLISH_EVERY_S = 60


def build_main(bot: Any) -> Dict[str, Any]:
    from modules import admin_inspect as ai
    snap = ai.bot_snapshot(bot)
    running, stopped = ai.loop_report(bot)
    errors, warnings = ai.error_counts()
    return {**snap, "loops_running": running, "loops_stopped": stopped,
            "errors_window": errors, "warnings_window": warnings, "window_s": ai.ERROR_WINDOW_S}


def build_logs() -> List[Dict[str, Any]]:
    from modules import admin_ops
    out = []
    for e in admin_ops.recent_logs(limit=LOG_LINES, mode="warnings"):
        out.append({"ts": e.ts, "level": "ERROR" if e.level >= 40 else "WARNING",
                    "logger": admin_ops.mask_secrets(e.logger_name)[:60],
                    "message": admin_ops.mask_secrets(" ".join(str(e.message).split()))[:LOG_MSG_MAX]})
    return out


def build_config() -> List[Dict[str, Any]]:
    import config
    from modules import admin_ops
    return [{"name": c.name, "shown": admin_ops.mask_secrets(c.shown), "secret": c.secret}
            for c in admin_ops.config_entries(config)]


async def publish(bot: Any, db: Any) -> None:
    """Write all three snapshots. One failing key never blocks the others."""
    for key, build in ((KEY_MAIN, lambda: build_main(bot)),
                       (KEY_LOGS, lambda: {"lines": build_logs()}),
                       (KEY_CONFIG, lambda: {"entries": build_config()})):
        try:
            payload = build()
            payload = {**payload, "published_at": time.time()} if isinstance(payload, dict) else payload
            await db.bot_snapshot_put(key, payload)
        except Exception:
            logger.warning("[snapshot] couldn't publish %r", key, exc_info=True)


def age_seconds(updated_at, now=None) -> float:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - updated_at).total_seconds())
