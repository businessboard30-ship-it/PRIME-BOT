"""XP and level gain (Phase 4).

* ``grant_xp`` is the ONLY code that changes ``catch_owned.xp`` or ``catch_owned.level``.
* ``catch_owned.xp`` is progress inside the current level (it resets on a level-up), so a
  creature caught at level 15 with 0 xp is valid. ``xp_to_next`` is the size of the bar.
* One transaction: lock the player row (serialises the per-day cap for that player), lock the
  creature row (scoped by id, user_id and clone_key in SQL, so a forged or stale id can never
  level someone else's creature), replay check, cap check, guarded UPDATE, audit row ``xp``.
* Idempotent: every grant carries an ``idem_key``. A retry with the same key for the same
  player returns the first result and changes nothing.
* Capped: each source has a per-grant maximum and a per-player per-UTC-day budget, counted
  from the audit rows of that source. A grant that would pass the budget is cut down to what
  is left; with nothing left it is refused with reason ``capped``.
* Refusals return ``XpResult(ok=False, reason=...)`` and write nothing. If the creature row
  changes under the guarded UPDATE, the grant raises and rolls back (never half applied).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from modules import catch_db, catch_gate
from modules.catch_game import LEVEL_MAX

# source -> (max xp per single grant, max xp per player per UTC day)
SOURCES: dict[str, tuple[int, int]] = {
    "catch": (40, 800),
    "buddy": (40, 600),
    "daily": (100, 300),
}

BUDDY_CATCH_XP = 10
BUDDY_NEW_SPECIES_XP = 15


def xp_to_next(level: int) -> int:
    """XP needed to go from ``level`` to ``level + 1``. 0 at the level cap."""
    if level >= LEVEL_MAX:
        return 0
    if level < 1:
        raise ValueError("level must be >= 1")
    return 30 + 2 * level * level


def apply_xp(level: int, xp: int, amount: int) -> tuple[int, int]:
    """Pure: add ``amount`` to (level, xp) and return the new (level, xp)."""
    if amount < 0:
        raise ValueError("amount must be >= 0")
    if level >= LEVEL_MAX:
        return LEVEL_MAX, 0
    xp += amount
    while level < LEVEL_MAX and xp >= xp_to_next(level):
        xp -= xp_to_next(level)
        level += 1
    if level >= LEVEL_MAX:
        xp = 0
    return level, xp


@dataclass(frozen=True, slots=True)
class XpResult:
    ok: bool
    reason: str | None = None  # blocked, bad_source, bad_amount, not_found, max_level, capped
    gained: int = 0
    level_before: int = 0
    level_after: int = 0
    xp: int = 0
    replay: bool = False

    @property
    def leveled(self) -> bool:
        return self.ok and self.level_after > self.level_before


def _from_detail(detail: dict) -> XpResult:
    return XpResult(
        True, None, int(detail.get("gained", 0)), int(detail.get("level_before", 0)),
        int(detail.get("level_after", 0)), int(detail.get("xp_after", 0)), replay=True,
    )


async def grant_xp(
    owned_id: int, user_id: int, clone_id: int | None, *, source: str, amount: int,
    idem_key: str, guild_id: int | None = None, conn=None,
) -> XpResult:
    """Give one creature XP. Never raises for a refusal; returns ok=False with a reason."""
    if source not in SOURCES:
        return XpResult(False, "bad_source")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return XpResult(False, "bad_amount")
    if not idem_key or len(idem_key) > 200:
        return XpResult(False, "bad_amount")
    per_grant, per_day = SOURCES[source]
    requested = min(amount, per_grant)
    key = catch_db.clone_key(clone_id)

    async with catch_db.transaction(conn) as c:
        gate = await catch_gate.check_player_allowed(user_id, guild_id, "catch", clone_id, conn=c)
        if not gate.allowed:
            return XpResult(False, "blocked")
        # Lock order is always player row, then creature row.
        locked_player = await c.fetchval(
            "SELECT 1 FROM catch_players WHERE user_id = $1 AND clone_key = $2 FOR UPDATE",
            user_id, key,
        )
        if locked_player is None:
            return XpResult(False, "not_found")
        prior = await c.fetchval(
            "SELECT detail FROM catch_audit WHERE action = 'xp' AND user_id = $1 "
            "AND (clone_id IS NOT DISTINCT FROM $2::int) AND detail->>'key' = $3 "
            "ORDER BY id LIMIT 1",
            user_id, clone_id, idem_key,
        )
        if prior is not None:
            detail = json.loads(prior) if isinstance(prior, str) else dict(prior)
            return _from_detail(detail)
        row = await c.fetchrow(
            "SELECT level, xp FROM catch_owned "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 FOR UPDATE",
            owned_id, user_id, key,
        )
        if row is None:
            return XpResult(False, "not_found")
        level, xp = int(row["level"]), int(row["xp"])
        if level >= LEVEL_MAX:
            return XpResult(False, "max_level", 0, level, level, 0)
        used = int(await c.fetchval(
            "SELECT COALESCE(SUM((detail->>'gained')::int), 0) FROM catch_audit "
            "WHERE action = 'xp' AND user_id = $1 AND (clone_id IS NOT DISTINCT FROM $2::int) "
            "AND detail->>'source' = $3 "
            "AND at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
            user_id, clone_id, source,
        ) or 0)
        gained = min(requested, per_day - used)
        if gained <= 0:
            return XpResult(False, "capped", 0, level, level, xp)
        new_level, new_xp = apply_xp(level, xp, gained)
        updated = await c.fetchval(
            "UPDATE catch_owned SET level = $4, xp = $5 "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 AND level = $6 AND xp = $7 "
            "RETURNING id",
            owned_id, user_id, key, new_level, new_xp, level, xp,
        )
        if updated is None:
            raise RuntimeError("creature changed during xp grant; rolled back")
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, ref_id, detail) "
            "VALUES ($1, $1, $2, $3, 'xp', $4, $5::jsonb)",
            user_id, clone_id, guild_id, owned_id,
            json.dumps({
                "key": idem_key, "source": source, "requested": amount, "gained": gained,
                "level_before": level, "level_after": new_level, "xp_after": new_xp,
            }),
        )
    return XpResult(True, None, gained, level, new_level, new_xp)


async def grant_buddy_catch_xp(
    user_id: int, clone_id: int | None, *, caught_owned_id: int, new_species: bool = False,
    guild_id: int | None = None, conn=None,
) -> XpResult | None:
    """XP for the buddy after a catch. None when the player has no usable buddy.

    The key is derived from the caught creature, so a retried catch callback can never pay twice.
    """
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        buddy_id = await db.fetchval(
            "SELECT o.id FROM catch_players p JOIN catch_owned o "
            "ON o.id = p.buddy_id AND o.user_id = p.user_id AND o.clone_key = p.clone_key "
            "WHERE p.user_id = $1 AND p.clone_key = $2",
            user_id, key,
        )
    if buddy_id is None:
        return None
    amount = BUDDY_CATCH_XP + (BUDDY_NEW_SPECIES_XP if new_species else 0)
    return await grant_xp(
        int(buddy_id), user_id, clone_id, source="buddy", amount=amount,
        idem_key=f"buddy-catch:{caught_owned_id}", guild_id=guild_id, conn=conn,
    )


__all__ = [
    "BUDDY_CATCH_XP", "BUDDY_NEW_SPECIES_XP", "SOURCES", "XpResult", "apply_xp",
    "grant_buddy_catch_xp", "grant_xp", "xp_to_next",
]
