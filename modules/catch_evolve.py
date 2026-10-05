"""Evolution (Phase 3 slice of P4-02): rules, preview and the one write path.

* ``rule_for`` / ``evaluate`` are pure: they read the species file and decide whether a
  creature may evolve (level trigger, item trigger, or already final).
* ``preview`` is read-only and shows before and after stats.
* ``evolve`` is the ONLY code that changes ``catch_owned.species_id``. One transaction:
  lock the creature row (scoped by id, user_id and clone_key), re-check the rule against the
  database level, lock and spend the evolution item if the species needs one, switch the
  species with a guarded ``UPDATE ... WHERE species_id = <old>`` (so a second tap finds
  nothing to change), register the new species in the Dex, and write an ``evolve`` audit row.
  The creature keeps its id, level, XP, nickname, favourite, lock, shiny/special flags and
  IVs. Evolving is NOT a catch: ``catch_players.total_catches`` and streaks are untouched.
* The player's buddy flag is kept, because the buddy is the same creature row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from modules import catch_db
from modules.catch_game import compute_stats
from modules.catch_species import all_species


@dataclass(frozen=True, slots=True)
class EvolutionRule:
    from_id: int
    to_id: int
    level: int | None
    item: str | None


@dataclass(frozen=True, slots=True)
class Eligibility:
    status: str  # ready, final_form, level_too_low, needs_item, no_trigger, unknown_target
    rule: EvolutionRule | None = None
    needed_level: int | None = None
    needed_item: str | None = None


@dataclass(frozen=True, slots=True)
class EvolutionPreview:
    owned_id: int
    level: int
    from_name: str
    to_name: str
    eligibility: Eligibility
    before: dict[str, int]
    after: dict[str, int]
    nickname: str | None
    item_owned: int = 0


@dataclass(frozen=True, slots=True)
class EvolveResult:
    ok: bool
    reason: str | None = None  # not_found, final_form, level_too_low, needs_item, no_trigger,
    #                            unknown_target, changed, no_item
    from_name: str = ""
    to_name: str = ""
    to_species_id: int = 0
    new_dex_entry: bool = False


def rule_for(species_id: int) -> EvolutionRule | None:
    """The species' evolution rule, or None for a final form."""
    species = all_species().get(int(species_id))
    if not species or not species.get("evolves_to"):
        return None
    return EvolutionRule(
        int(species_id), int(species["evolves_to"]),
        int(species["evolve_level"]) if species.get("evolve_level") else None,
        str(species["evolve_item"]) if species.get("evolve_item") else None,
    )


def evaluate(species_id: int, level: int, item_quantity: int = 0) -> Eligibility:
    """Pure decision: may a creature of this species and level evolve right now?"""
    rule = rule_for(species_id)
    if rule is None:
        return Eligibility("final_form")
    if rule.to_id not in all_species():
        return Eligibility("unknown_target", rule)
    if rule.level is None and rule.item is None:
        return Eligibility("no_trigger", rule)
    if rule.level is not None and int(level) < rule.level:
        return Eligibility("level_too_low", rule, needed_level=rule.level, needed_item=rule.item)
    if rule.item is not None and int(item_quantity) < 1:
        return Eligibility("needs_item", rule, needed_level=rule.level, needed_item=rule.item)
    return Eligibility("ready", rule, needed_level=rule.level, needed_item=rule.item)


def _stats(species_id: int, ivs, level: int) -> dict[str, int]:
    species = all_species()[int(species_id)]
    return compute_stats([int(v) for v in species["base_stats"]], [int(v) for v in ivs], level)


async def preview(owned_id: int, user_id: int, clone_id: int | None, *, conn=None) -> EvolutionPreview | None:
    """Before/after view for one creature. Read-only; None if it isn't theirs."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        row = await db.fetchrow(
            "SELECT id, species_id, level, ivs, nickname FROM catch_owned "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3",
            owned_id, user_id, key,
        )
        if row is None or int(row["species_id"]) not in all_species():
            return None
        rule = rule_for(int(row["species_id"]))
        owned_qty = 0
        if rule is not None and rule.item is not None:
            owned_qty = int(await db.fetchval(
                "SELECT quantity FROM catch_inventory WHERE user_id = $1 AND clone_key = $2 AND item_key = $3",
                user_id, key, rule.item,
            ) or 0)
    species = all_species()[int(row["species_id"])]
    level = int(row["level"])
    eligibility = evaluate(int(row["species_id"]), level, owned_qty)
    target = all_species().get(rule.to_id) if rule else None
    return EvolutionPreview(
        owned_id=int(row["id"]), level=level, from_name=str(species["name"]),
        to_name=str(target["name"]) if target else "",
        eligibility=eligibility,
        before=_stats(int(row["species_id"]), row["ivs"], level),
        after=_stats(rule.to_id, row["ivs"], level) if (rule and target) else {},
        nickname=row["nickname"], item_owned=owned_qty,
    )


async def _evolve_tx(
    owned_id: int, user_id: int, clone_id: int | None, *, guild_id: int | None = None, conn=None,
) -> EvolveResult:
    key = catch_db.clone_key(clone_id)
    async with catch_db.transaction(conn) as c:
        row = await c.fetchrow(
            "SELECT id, species_id, level, shiny FROM catch_owned "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 FOR UPDATE",
            owned_id, user_id, key,
        )
        if row is None:
            return EvolveResult(False, "not_found")
        old_id = int(row["species_id"])
        old = all_species().get(old_id)
        if old is None:
            return EvolveResult(False, "unknown_target")
        rule = rule_for(old_id)
        item_qty = 0
        if rule is not None and rule.item is not None:
            item_qty = int(await c.fetchval(
                "SELECT quantity FROM catch_inventory WHERE user_id = $1 AND clone_key = $2 AND item_key = $3 FOR UPDATE",
                user_id, key, rule.item,
            ) or 0)
        verdict = evaluate(old_id, int(row["level"]), item_qty)
        if verdict.status != "ready" or verdict.rule is None:
            return EvolveResult(False, verdict.status, from_name=str(old["name"]))
        target = all_species()[verdict.rule.to_id]
        enabled = await c.fetchval(
            "SELECT enabled FROM catch_species WHERE id = $1", verdict.rule.to_id,
        )
        if not enabled:
            return EvolveResult(False, "unknown_target", from_name=str(old["name"]))
        if verdict.rule.item is not None:
            spent = await c.fetchval(
                "UPDATE catch_inventory SET quantity = quantity - 1, updated_at = now() "
                "WHERE user_id = $1 AND clone_key = $2 AND item_key = $3 AND quantity >= 1 RETURNING quantity",
                user_id, key, verdict.rule.item,
            )
            if spent is None:
                return EvolveResult(False, "no_item", from_name=str(old["name"]))
        switched = await c.fetchval(
            "UPDATE catch_owned SET species_id = $4 "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 AND species_id = $5 RETURNING id",
            owned_id, user_id, key, verdict.rule.to_id, old_id,
        )
        if switched is None:  # the species changed under us; nothing was spent (rolled back below)
            raise _Refused("changed")
        shiny = 1 if row["shiny"] else 0
        caught_count = await c.fetchval(
            """
            INSERT INTO catch_dex (user_id, clone_id, species_id, seen, caught_count, shiny_caught, first_caught_at)
            VALUES ($1, $2, $3, TRUE, 1, $4, now())
            ON CONFLICT (user_id, clone_key, species_id) DO UPDATE SET
                seen = TRUE,
                caught_count = GREATEST(catch_dex.caught_count, 1),
                shiny_caught = GREATEST(catch_dex.shiny_caught, EXCLUDED.shiny_caught),
                first_caught_at = COALESCE(catch_dex.first_caught_at, now())
            RETURNING (xmax = 0) AS inserted
            """,
            user_id, clone_id, verdict.rule.to_id, shiny,
        )
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, ref_id, detail) "
            "VALUES ($1, $1, $2, $3, 'evolve', $4, $5::jsonb)",
            user_id, clone_id, guild_id, owned_id,
            json.dumps({
                "from": old_id, "to": verdict.rule.to_id, "level": int(row["level"]),
                "item": verdict.rule.item,
            }),
        )
    return EvolveResult(
        True, from_name=str(old["name"]), to_name=str(target["name"]),
        to_species_id=verdict.rule.to_id, new_dex_entry=bool(caught_count),
    )


class _Refused(Exception):
    """Raised inside the transaction to roll back a half-done evolve."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def evolve(
    owned_id: int, user_id: int, clone_id: int | None, *, guild_id: int | None = None, conn=None,
) -> EvolveResult:
    """Evolve one creature. Never raises for a refusal; returns ok=False with a reason.

    A race detected mid-transaction rolls the whole transaction back (an evolution item that
    was already spent is returned) and comes back as the ``changed`` refusal.
    """
    try:
        return await _evolve_tx(owned_id, user_id, clone_id, guild_id=guild_id, conn=conn)
    except _Refused as refused:
        return EvolveResult(False, refused.reason)


__all__ = [
    "Eligibility", "EvolutionPreview", "EvolutionRule", "EvolveResult", "evaluate", "evolve",
    "preview", "rule_for",
]
