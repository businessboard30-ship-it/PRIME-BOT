# path: modules/test_harness.py

"""
Bot test harness -- pure gate logic (phase 1 of docs/BOT_TEST_HARNESS_PLAN.md).

This module decides ONE thing: may the harness be offered / run right now? It touches no Discord objects and no
database, so it is trivially unit-testable. Every layer (offer gate, execution gate, cog handler gate) calls
`check_gates`; a refusal from any one of them is final.

Rules enforced here (plan, "Hard rules" 1, 3, 4):
  * ONE server: `config.TEST_GUILD_ID`. 0 / empty / unset = the feature is disabled everywhere.
  * Owner only: the asker must be in `config.DISCORD_CLONE_ADMIN_IDS`.
  * Main bot only: a clone (clone_id is not None) never runs it.

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


def is_enabled() -> bool:
    return test_guild_id() > 0


def check_gates(guild_id, user_id, clone_id=None, *, test_guild: Optional[int] = None,
                admin_ids: Optional[Iterable[int]] = None) -> Optional[str]:
    """Return None when allowed, else a short internal reason (never shown to the asker verbatim).

    `test_guild` / `admin_ids` exist so tests can inject values; production callers leave them None so the live
    config is read on every call (never cached), which keeps the empty-ID kill switch immediate."""
    configured = _int_or_zero(test_guild) if test_guild is not None else test_guild_id()
    if configured <= 0:
        return "harness disabled (TEST_GUILD_ID unset)"
    if clone_id is not None:
        return "clones never run the harness"
    if guild_id is None or _int_or_zero(guild_id) != configured:
        return "not the test guild"
    admins = set(admin_ids) if admin_ids is not None else set(getattr(config, "DISCORD_CLONE_ADMIN_IDS", ()) or ())
    if user_id is None or user_id not in admins:
        return "asker is not the owner"
    return None
