# path: modules/test_harness.py

"""
Bot test harness -- pure gate logic (phase 1 of docs/BOT_TEST_HARNESS_PLAN.md).

This module decides ONE thing: may the harness be offered / run right now? It touches no Discord objects and no
database, so it is trivially unit-testable. Every layer (offer gate, execution gate, cog handler gate) calls
`check_gates`; a refusal from any one of them is final.

Rules enforced here (plan, "Hard rules" 1, 3, 4):
  * ONE server: `config.TEST_GUILD_ID`. 0 / empty / unset = the feature is disabled everywhere.
  * Owner only: the asker must be in `config.DISCORD_CLONE_ADMIN_IDS`.
  * Harness bots only: the main bot, or -- when `config.TEST_CLONE_IDS` is non-empty -- exactly those clones and nobody else (the main bot
    then refuses). Every other clone never runs it. Runs are owned by the bot that started them (database.py).

Nothing destructive lives in this phase.
"""

from typing import Iterable, Optional

import config


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def test_guild_id() -> int:
    """The configured test guild ID, or 0 when the feature is off."""
    return _int_or_zero(getattr(config, "TEST_GUILD_ID", 0))


def test_clone_ids() -> frozenset:
    """The clones allowed to run the harness (empty = the main bot is the harness bot)."""
    raw = getattr(config, "TEST_CLONE_IDS", ()) or ()
    return frozenset(n for n in (_int_or_zero(x) for x in raw) if n > 0)


def bot_may_run(clone_id, *, test_clones: Optional[Iterable[int]] = None) -> bool:
    """Is THIS bot one of the bots the harness may run on? clone_id is None for the main bot."""
    wanted = (frozenset(n for n in (_int_or_zero(x) for x in test_clones) if n > 0)
              if test_clones is not None else test_clone_ids())
    if wanted:
        return clone_id is not None and _int_or_zero(clone_id) in wanted
    return clone_id is None


def is_enabled() -> bool:
    return test_guild_id() > 0


def check_gates(guild_id, user_id, clone_id=None, *, test_guild: Optional[int] = None,
                admin_ids: Optional[Iterable[int]] = None, test_clones: Optional[Iterable[int]] = None) -> Optional[str]:
    """Return None when allowed, else a short internal reason (never shown to the asker verbatim).

    `test_guild` / `admin_ids` exist so tests can inject values; production callers leave them None so the live
    config is read on every call (never cached), which keeps the empty-ID kill switch immediate."""
    configured = _int_or_zero(test_guild) if test_guild is not None else test_guild_id()
    if configured <= 0:
        return "harness disabled (TEST_GUILD_ID unset)"
    if not bot_may_run(clone_id, test_clones=test_clones):
        return "this bot is not the harness bot"
    if guild_id is None or _int_or_zero(guild_id) != configured:
        return "not the test guild"
    admins = set(admin_ids) if admin_ids is not None else set(getattr(config, "DISCORD_CLONE_ADMIN_IDS", ()) or ())
    if user_id is None or user_id not in admins:
        return "asker is not the owner"
    return None


# ── run state (phase 2) ───────────────────────────────────────────────────
# A run is one snapshot plus its dead-man deadline. States: armed -> restoring -> restored | restore_failed.
# ("running" is reserved for the scenario phases; it is treated exactly like "armed" for the timer and recovery.)
DEADMAN_DEFAULT_MINUTES = 15
DEADMAN_MAX_MINUTES = 60
UNFINISHED = ("armed", "running", "restoring")

CANNOT_RESTORE = ("Not restorable by design: message history and deleted messages, bans of real users, webhook tokens, "
                  "integrations, threads, member role assignments, managed (bot/integration) roles, and channel types "
                  "other than text, announcement, voice, stage and forum.")


def clamp_minutes(value) -> int:
    n = _int_or_zero(value)
    if n <= 0:
        return DEADMAN_DEFAULT_MINUTES
    return min(n, DEADMAN_MAX_MINUTES)


def check_guild(guild_id, clone_id=None, *, test_guild: Optional[int] = None, test_clones: Optional[Iterable[int]] = None) -> Optional[str]:
    """Gate for code that has no interaction (the dead-man timer, restart recovery): enabled, the harness bot, test guild.
    There is no asker to check; these paths only ever restore a run the owner already armed."""
    configured = _int_or_zero(test_guild) if test_guild is not None else test_guild_id()
    if configured <= 0:
        return "harness disabled (TEST_GUILD_ID unset)"
    if not bot_may_run(clone_id, test_clones=test_clones):
        return "this bot is not the harness bot"
    if guild_id is None or _int_or_zero(guild_id) != configured:
        return "not the test guild"
    return None


def format_report(result: dict, *, why: str) -> str:
    """Plain-text restore report posted in the test server (and kept in the DB)."""
    head = "✅ **Restore verified**" if result.get("ok") else "⚠️ **Restore incomplete**"
    lines = [f"{head} ({why}). Applied {result.get('done', 0)} of {result.get('planned', 0)} planned change(s)."]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    for label, key in (("Failed", "failed"), ("Skipped", "skipped")):
        items = result.get(key) or []
        if items:
            lines.append(f"{label} ({len(items)}):")
            lines += [f"• {a} — {b}" for a, b in items[:8]]
            if len(items) > 8:
                lines.append(f"• …and {len(items) - 8} more")
    rem = result.get("remaining") or []
    if rem:
        lines.append(f"Still different from the snapshot ({len(rem)}):")
        lines += [f"• {r}" for r in rem[:8]]
        if len(rem) > 8:
            lines.append(f"• …and {len(rem) - 8} more")
    for n in (result.get("notes") or [])[:5]:
        lines.append(f"• note: {n}")
    lines.append(CANNOT_RESTORE)
    return "\n".join(lines)[:1990]
