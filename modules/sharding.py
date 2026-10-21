"""Shard configuration and health helpers for the Discord bot.

The bot runs as ONE process using discord.py's AutoShardedBot. discord.py asks Discord
(GET /gateway/bot) how many shards it needs when it starts, so nothing here hardcodes a count.
Everything in this module is pure (no Discord or database calls) so it can be tested directly.

Discord makes sharding mandatory at 2,500 guilds. The recommendation Discord returns is about
1,000 guilds per shard, so the automatic count grows on its own as the bot grows.
See docs/SHARDING.md for the beginner-friendly guide.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Discord's documented limit: a bot in 2,500 or more guilds MUST shard.
MANDATORY_SHARD_THRESHOLD = 2500
# Warn well before a single shard gets close to that per-shard limit.
PER_SHARD_WARN_AT = 2000

ENV_SHARD_COUNT = "PRIME_SHARD_COUNT"
MAX_SHARD_COUNT = 1000  # sanity cap on the override, far above anything Discord recommends here


class ShardConfigError(RuntimeError):
    """A bad sharding setting. The message is safe to log (it never contains secrets)."""


@dataclass(frozen=True)
class ShardConfig:
    shard_count: Optional[int]  # None = let discord.py ask Discord for the recommended count
    source: str                 # "auto" or "env"

    @property
    def is_auto(self) -> bool:
        return self.shard_count is None

    def describe(self) -> str:
        return "automatic (Discord's recommended count)" if self.is_auto else f"fixed at {self.shard_count} (from {ENV_SHARD_COUNT})"


def resolve_shard_config(env: Optional[Mapping[str, str]] = None, clone_id: Optional[int] = None) -> ShardConfig:
    """Pick the shard configuration.

    Default is automatic. PRIME_SHARD_COUNT is an optional override for the MAIN bot only, to be
    used when Discord support or an operator needs a specific number. A bad value stops startup
    with a clear message rather than silently falling back. Clones always use automatic.
    """
    if clone_id is not None:
        return ShardConfig(None, "auto")
    env = os.environ if env is None else env
    raw = (env.get(ENV_SHARD_COUNT) or "").strip().lower()
    if raw in ("", "auto"):
        return ShardConfig(None, "auto")
    try:
        count = int(raw)
    except ValueError:
        raise ShardConfigError(f"{ENV_SHARD_COUNT} must be a whole number or 'auto' (got a non-number).") from None
    if not 1 <= count <= MAX_SHARD_COUNT:
        raise ShardConfigError(f"{ENV_SHARD_COUNT} must be between 1 and {MAX_SHARD_COUNT} (got {count}).")
    return ShardConfig(count, "env")


@dataclass(frozen=True)
class ShardStatus:
    shard_id: int
    guilds: int
    latency_ms: Optional[int]
    closed: bool
    ratelimited: bool

    @property
    def healthy(self) -> bool:
        return not self.closed


def _latency_ms(value: Any) -> Optional[int]:
    try:
        if value is None or value != value or value in (float("inf"), float("-inf")):
            return None
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def snapshot(bot: Any) -> List[ShardStatus]:
    """Per-shard status. A bot that is not sharded reports one shard (id 0)."""
    guild_counts: dict = {}
    for guild in list(getattr(bot, "guilds", []) or []):
        sid = getattr(guild, "shard_id", None)
        sid = 0 if sid is None else sid
        guild_counts[sid] = guild_counts.get(sid, 0) + 1
    shards = getattr(bot, "shards", None)
    if not shards:
        return [ShardStatus(0, guild_counts.get(0, 0), _latency_ms(getattr(bot, "latency", None)), False, False)]
    out = []
    for sid in sorted(shards):
        info = shards[sid]
        try:
            closed = bool(info.is_closed())
        except Exception:
            closed = True
        try:
            limited = bool(info.is_ws_ratelimited())
        except Exception:
            limited = False
        out.append(ShardStatus(sid, guild_counts.get(sid, 0), _latency_ms(getattr(info, "latency", None)), closed, limited))
    return out


def capacity_warning(total_guilds: int, shard_count: int) -> Optional[str]:
    """A warning when the bot is crowding its shards, or None when all is well."""
    shard_count = max(1, int(shard_count or 1))
    per_shard = total_guilds / shard_count
    if total_guilds >= MANDATORY_SHARD_THRESHOLD and shard_count == 1:
        return (f"{total_guilds:,} guilds on 1 shard: Discord requires sharding at {MANDATORY_SHARD_THRESHOLD:,} guilds. "
                f"Remove {ENV_SHARD_COUNT} or set it to 'auto'.")
    if per_shard >= PER_SHARD_WARN_AT:
        return (f"About {per_shard:,.0f} guilds per shard is close to Discord's {MANDATORY_SHARD_THRESHOLD:,} limit. "
                "Restart so Discord's recommended shard count is re-read.")
    return None


def health_summary(statuses: List[ShardStatus]) -> dict:
    """Totals and an honest overall verdict: one down shard means NOT fully healthy."""
    down = [s.shard_id for s in statuses if not s.healthy]
    return {
        "shard_count": len(statuses),
        "total_guilds": sum(s.guilds for s in statuses),
        "down_shards": down,
        "ratelimited_shards": [s.shard_id for s in statuses if s.ratelimited],
        "all_healthy": not down,
    }


def format_report(statuses: List[ShardStatus], config: Optional[ShardConfig] = None, max_rows: int = 25) -> str:
    """Owner-facing text report. Counts and shard ids only: no tokens, no exception text."""
    s = health_summary(statuses)
    verdict = "✅ all shards connected" if s["all_healthy"] else f"⚠️ shard(s) down: {', '.join(map(str, s['down_shards']))}"
    lines = [f"**Shards:** {s['shard_count']}  •  **Guilds:** {s['total_guilds']:,}  •  {verdict}"]
    if config is not None:
        lines.append(f"-# Shard count setting: {config.describe()}")
    for st in statuses[:max_rows]:
        flag = "🔴 down" if st.closed else ("🟡 rate-limited" if st.ratelimited else "🟢 up")
        lat = f"{st.latency_ms} ms" if st.latency_ms is not None else "n/a"
        lines.append(f"`#{st.shard_id}` {flag} • {st.guilds:,} guilds • {lat}")
    if len(statuses) > max_rows:
        lines.append(f"-# …and {len(statuses) - max_rows} more shard(s)")
    warn = capacity_warning(s["total_guilds"], s["shard_count"])
    if warn:
        lines.append(f"⚠️ {warn}")
    return "\n".join(lines)
