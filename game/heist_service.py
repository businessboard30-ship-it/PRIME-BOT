"""
Heist Wars — application/service layer.

Everything that touches the database lives here. `heist_engine`,
`decision_engine` and `reward_engine` stay pure; this module is what wires
them to Postgres and is the only thing the Discord cog is allowed to call
into for game logic (see discord_bot/cogs/heist.py).

Security/consistency guarantees this module is responsible for (brief
sections 10-14, 17, 21):

  * Ownership: every mutating call takes `user_id` from the authenticated
    Discord interaction and checks it against the run's owner in the DB —
    never trusts a custom_id-embedded value on its own.
  * Atomic completion: `complete_heist` performs the ESCAPE -> COMPLETED/
    FAILED transition with a single conditional UPDATE
    (`WHERE status = 'ESCAPE'`) plus a UNIQUE(run_id) ledger insert inside
    one transaction. A second, concurrent, or retried call for the same
    run never pays out twice — it just returns the already-finalized row.
  * Randomness: the final random_modifier is generated exactly once (at
    the moment `complete_heist` wins the atomic transition) and persisted,
    so retries reconstruct the same outcome instead of re-rolling.
  * Cooldowns are enforced here against `heist_runs.completed_at`, not by
    the Discord UI.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import get_pool
from game import heist_engine, item_engine, item_service, reward_engine
from game.decision_engine import NoEligibleEventError, next_phase, resolve_event_choice, select_event
from game.locations import get_location
from game.models import EVENT_PHASES, Approach, HeistState, is_valid_transition

logger = logging.getLogger(__name__)

# An active-but-abandoned run (bot restarted mid-heist, user vanished) is
# treated as expired after this long so it stops blocking a new /heist.
ABANDONED_RUN_TIMEOUT = timedelta(hours=1)


class HeistServiceError(Exception):
    """Base class for expected, user-facing service errors."""


class NotOwnerError(HeistServiceError):
    pass


class NoActiveRunError(HeistServiceError):
    pass


class InvalidStateError(HeistServiceError):
    pass


class CooldownActiveError(HeistServiceError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"cooldown active, retry after {retry_after_seconds}s")


class LevelTooLowError(HeistServiceError):
    pass


class AlreadyActiveRunError(HeistServiceError):
    pass


class InvalidLocationError(HeistServiceError):
    pass


class InvalidChoiceError(HeistServiceError):
    pass


def _identity_key(clone_id: Optional[int]) -> int:
    # Matches the COALESCE(clone_id, -1) convention used across the schema.
    return clone_id if clone_id is not None else -1


async def _log_event(conn, run_id: int, action: str, *, phase: str | None = None,
                      event_key: str | None = None, choice_key: str | None = None,
                      modifier: int | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO heist_events_log (run_id, action, phase, event_key, choice_key, modifier)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        run_id, action, phase, event_key, choice_key, modifier,
    )


async def _get_or_create_player(conn, guild_id: int, clone_id: Optional[int], user_id: int) -> dict:
    row = await conn.fetchrow(
        """
        SELECT * FROM heist_players
        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
        """,
        guild_id, clone_id, user_id,
    )
    if row:
        return dict(row)
    row = await conn.fetchrow(
        """
        INSERT INTO heist_players (guild_id, clone_id, user_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET updated_at = heist_players.updated_at
        RETURNING *
        """,
        guild_id, clone_id, user_id,
    )
    return dict(row)


async def get_active_run(guild_id: int, clone_id: Optional[int], user_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM heist_runs
            WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
              AND status NOT IN ('COMPLETED', 'FAILED', 'EXPIRED')
            """,
            guild_id, clone_id, user_id,
        )
        return dict(row) if row else None


async def _get_run_or_raise(conn, run_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM heist_runs WHERE id = $1", run_id)
    if not row:
        raise NoActiveRunError(f"run {run_id} does not exist")
    return dict(row)


def _check_ownership(run: dict, user_id: int) -> None:
    if run["user_id"] != user_id:
        raise NotOwnerError("this heist belongs to someone else")


async def start_heist(
    guild_id: int,
    clone_id: Optional[int],
    user_id: int,
    location_key: str,
    approach: str,
) -> dict:
    """Validates level + cooldown + no-active-run, creates the run, and
    immediately advances it to INFILTRATION with the first event selected.
    Returns the resulting run row (as dict) plus the first event."""
    location = get_location(location_key)
    if location is None:
        raise InvalidLocationError(f"unknown location {location_key!r}")
    try:
        approach_enum = Approach(approach)
    except ValueError:
        raise InvalidChoiceError(f"unknown approach {approach!r}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            player = await _get_or_create_player(conn, guild_id, clone_id, user_id)
            if player["level"] < location.required_level:
                raise LevelTooLowError(
                    f"requires level {location.required_level}, player is level {player['level']}"
                )

            # Expire any stale abandoned run first so it doesn't block a
            # legitimate new attempt (brief §21).
            await _expire_abandoned_run(conn, guild_id, clone_id, user_id)

            existing = await conn.fetchrow(
                """
                SELECT id FROM heist_runs
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
                  AND status NOT IN ('COMPLETED', 'FAILED', 'EXPIRED')
                """,
                guild_id, clone_id, user_id,
            )
            if existing:
                raise AlreadyActiveRunError("you already have an active heist")

            cooldown_row = await conn.fetchrow(
                """
                SELECT completed_at FROM heist_runs
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
                  AND location_key = $4 AND status IN ('COMPLETED', 'FAILED')
                ORDER BY completed_at DESC LIMIT 1
                """,
                guild_id, clone_id, user_id, location_key,
            )
            if cooldown_row and cooldown_row["completed_at"] is not None:
                elapsed = datetime.now(timezone.utc) - cooldown_row["completed_at"]
                remaining = timedelta(seconds=location.cooldown_seconds) - elapsed
                if remaining.total_seconds() > 0:
                    raise CooldownActiveError(retry_after_seconds=int(remaining.total_seconds()))

            run_row = await conn.fetchrow(
                """
                INSERT INTO heist_runs
                    (guild_id, clone_id, user_id, location_key, approach, level_at_start, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'PLANNING')
                RETURNING *
                """,
                guild_id, clone_id, user_id, location_key, approach_enum.value, player["level"],
            )
            run = dict(run_row)
            await conn.execute(
                "INSERT INTO heist_participants (run_id, user_id, role) VALUES ($1, $2, 'owner')",
                run["id"], user_id,
            )
            await _log_event(conn, run["id"], "heist_started", phase="PLANNING")
            await _log_event(conn, run["id"], "approach_selected", phase="PLANNING", choice_key=approach_enum.value)

            run = await _advance_to_next_event_phase(conn, run)
            return run


async def _advance_to_next_event_phase(conn, run: dict) -> dict:
    """Transitions run['status'] forward to the next EVENT_PHASES entry (or
    ESCAPE if all three are done) and, if it's an event phase, selects and
    logs the event for it. Caller must be inside a transaction."""
    current = HeistState(run["status"])
    target = next_phase(current)
    if not is_valid_transition(current, target):
        raise InvalidStateError(f"cannot advance {current} -> {target}")

    approach = Approach(run["approach"])

    events_used = {
        r["event_key"] for r in await conn.fetch(
            "SELECT event_key FROM heist_events_log WHERE run_id = $1 AND action = 'event_presented'",
            run["id"],
        )
    }

    updated = await conn.fetchrow(
        "UPDATE heist_runs SET status = $2, updated_at = NOW() WHERE id = $1 AND status = $3 RETURNING *",
        run["id"], target.value, current.value,
    )
    if not updated:
        raise InvalidStateError("concurrent modification detected during phase advance")
    run = dict(updated)
    await _log_event(conn, run["id"], "phase_changed", phase=target.value)

    if target in EVENT_PHASES:
        try:
            event = select_event(target, run["location_key"], approach, events_used)
        except NoEligibleEventError:
            logger.error("No eligible event for phase=%s location=%s approach=%s", target, run["location_key"], approach)
            raise
        await _log_event(conn, run["id"], "event_presented", phase=target.value, event_key=event.key)

    return run


async def choose_event(guild_id: int, clone_id: Optional[int], user_id: int, run_id: int, choice_key: str) -> dict:
    """Resolves the player's choice for the CURRENT event of the CURRENT
    phase, applies its modifier (or fails the run immediately on
    instant_fail), and advances to the next phase. Choices are permanent:
    only the run's single current, unresolved event can ever be answered
    (there's no "current event" pointer to rewind)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            run = await _get_run_or_raise(conn, run_id)
            _check_ownership(run, user_id)
            if run["guild_id"] != guild_id or _identity_key(run["clone_id"]) != _identity_key(clone_id):
                raise NotOwnerError("run does not belong to this guild/clone context")

            phase = HeistState(run["status"])
            if phase not in EVENT_PHASES:
                raise InvalidStateError(f"no event to answer in phase {phase}")

            last_event_row = await conn.fetchrow(
                """
                SELECT event_key FROM heist_events_log
                WHERE run_id = $1 AND action = 'event_presented' AND phase = $2
                ORDER BY id DESC LIMIT 1
                """,
                run_id, phase.value,
            )
            if not last_event_row:
                raise InvalidStateError("no event currently pending for this phase")
            already_answered = await conn.fetchrow(
                """
                SELECT 1 FROM heist_events_log
                WHERE run_id = $1 AND action = 'choice_selected' AND phase = $2
                """,
                run_id, phase.value,
            )
            if already_answered:
                raise InvalidStateError("this event has already been answered")

            from game.locations import get_event
            event = get_event(last_event_row["event_key"])
            resolution = resolve_event_choice(event, choice_key)

            await _log_event(
                conn, run_id, "choice_selected", phase=phase.value,
                event_key=event.key, choice_key=choice_key, modifier=resolution.choice.success_modifier,
            )

            if resolution.instant_fail:
                failed = await conn.fetchrow(
                    "UPDATE heist_runs SET status = 'FAILED', succeeded = FALSE, updated_at = NOW() "
                    "WHERE id = $1 AND status = $2 RETURNING *",
                    run_id, phase.value,
                )
                if not failed:
                    raise InvalidStateError("concurrent modification detected")
                await _log_event(conn, run_id, "heist_failed", phase=phase.value)
                return dict(failed)

            new_modifier = heist_engine.resolve_choice(run["decision_modifier_total"], resolution.choice.success_modifier)
            run = dict(await conn.fetchrow(
                "UPDATE heist_runs SET decision_modifier_total = $2, updated_at = NOW() WHERE id = $1 RETURNING *",
                run_id, new_modifier,
            ))

            run = await _advance_to_next_event_phase(conn, run)

            if HeistState(run["status"]) == HeistState.ESCAPE:
                run = await complete_heist(guild_id, clone_id, user_id, run_id, _conn=conn)

            return run


async def complete_heist(
    guild_id: int, clone_id: Optional[int], user_id: int, run_id: int, _conn=None,
) -> dict:
    """Atomically resolves the ESCAPE -> COMPLETED/FAILED transition and
    grants the reward exactly once. Safe to call multiple times (including
    concurrently) for the same run — only the request that wins the
    conditional UPDATE performs the roll/payout; every other caller
    (including this run's own re-entrant call from choose_event) just
    reads back the already-finalized row.
    """
    async def _run(conn):
        run = await _get_run_or_raise(conn, run_id)
        _check_ownership(run, user_id)
        if run["guild_id"] != guild_id or _identity_key(run["clone_id"]) != _identity_key(clone_id):
            raise NotOwnerError("run does not belong to this guild/clone context")

        if HeistState(run["status"]) in (HeistState.COMPLETED, HeistState.FAILED, HeistState.EXPIRED):
            # Already finalized — idempotent no-op, return existing result.
            return run

        if HeistState(run["status"]) != HeistState.ESCAPE:
            raise InvalidStateError(f"cannot complete a run in state {run['status']}")

        location = get_location(run["location_key"])
        random_modifier = random.randint(0, 99)  # generated exactly once, right here

        # Item modifier: sum each event phase's contextual tool bonus
        # (already individually capped by item_engine), then re-clamp the
        # combined total — belt-and-braces so no equipped combination can
        # ever push the run's item contribution past ITEM_MODIFIER_CAP.
        equipped = await item_service.equipped_gameplay_item_keys(guild_id, clone_id, user_id)
        item_modifier_total = 0
        for phase in EVENT_PHASES:
            item_modifier_total += item_engine.calculate_phase_item_modifier(equipped, phase).capped_total
        item_modifier_total = max(0, min(item_engine.ITEM_MODIFIER_CAP, item_modifier_total))

        breakdown = heist_engine.calculate_success_chance(
            location=location,
            approach=Approach(run["approach"]),
            player_level=run["level_at_start"],
            decision_modifier=run["decision_modifier_total"],
            item_modifier=item_modifier_total,
        )
        succeeded = heist_engine.determine_outcome(breakdown.final_chance, random_modifier)
        new_status = HeistState.COMPLETED if succeeded else HeistState.FAILED

        updated = await conn.fetchrow(
            """
            UPDATE heist_runs
            SET status = $2, succeeded = $3, random_modifier = $4, final_success_chance = $5,
                updated_at = NOW(), completed_at = NOW()
            WHERE id = $1 AND status = 'ESCAPE'
            RETURNING *
            """,
            run_id, new_status.value, succeeded, random_modifier, breakdown.final_chance,
        )
        if not updated:
            # Lost the race — someone else already finalized it. Read back
            # authoritative state instead of erroring.
            return await _get_run_or_raise(conn, run_id)
        run = dict(updated)
        await _log_event(conn, run_id, "outcome_resolved", phase="ESCAPE", modifier=random_modifier)

        reward = reward_engine.calculate_reward(location, succeeded)

        # Equipped-item reward/XP % bonuses (only meaningful on success —
        # a failed run's small consolation payout is not boosted) and a
        # possible bonus item drop. The drop roll happens here, inside the
        # same transaction guarded by the reward ledger's UNIQUE(run_id)
        # insert below, so a retried/concurrent completion can never grant
        # the item twice — the loser of that race just returns early.
        dropped_item = None
        if succeeded:
            reward_bonus_pct = item_engine.calculate_reward_modifier_pct(equipped)
            xp_bonus_pct = item_engine.calculate_xp_modifier_pct(equipped)
            reward = reward_engine.RewardResult(
                cash=reward.cash + (reward.cash * reward_bonus_pct) // 100,
                xp=reward.xp + (reward.xp * xp_bonus_pct) // 100,
                intel=reward.intel,
                reputation=reward.reputation,
            )
            dropped_item = item_engine.resolve_drop()

        try:
            await conn.execute(
                """
                INSERT INTO heist_reward_ledger
                    (run_id, guild_id, clone_id, user_id, cash, xp, intel, reputation, item_key, item_rarity)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                run_id, guild_id, clone_id, user_id, reward.cash, reward.xp, reward.intel, reward.reputation,
                dropped_item.key if dropped_item else None,
                dropped_item.rarity.value if dropped_item else None,
            )
        except Exception:
            # UNIQUE(run_id) violation means another concurrent transaction
            # already inserted the ledger row for this run — that's the
            # expected/safe outcome of the race, not an error to surface.
            logger.info("Reward ledger row for run %s already exists; skipping duplicate insert", run_id)
            return run

        if dropped_item is not None:
            await item_service.grant_item(
                conn, guild_id, clone_id, user_id, dropped_item.key,
                source_type="HEIST_REWARD", source_id=str(run_id),
            )

        await conn.execute(
            """
            UPDATE heist_runs
            SET reward_cash = $2, reward_xp = $3, reward_intel = $4, reward_reputation = $5, reward_granted = TRUE
            WHERE id = $1
            """,
            run_id, reward.cash, reward.xp, reward.intel, reward.reputation,
        )
        await conn.execute(
            """
            UPDATE heist_players
            SET xp = xp + $4, intel = intel + $5, reputation = reputation + $6, updated_at = NOW()
            WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
            """,
            guild_id, clone_id, user_id, reward.xp, reward.intel, reward.reputation,
        )
        await _apply_level_ups(conn, guild_id, clone_id, user_id)

        if reward.cash:
            await _grant_cash(conn, guild_id, clone_id, user_id, reward.cash, run_id)

        await _log_event(conn, run_id, "reward_granted", phase=new_status.value)
        await _log_event(conn, run_id, "heist_completed" if succeeded else "heist_failed", phase=new_status.value)

        run = dict(await conn.fetchrow("SELECT * FROM heist_runs WHERE id = $1", run_id))
        # Transient, not persisted on the run row itself (it already lives
        # on heist_reward_ledger) — attached here purely so the Discord UI
        # can show "ITEM ACQUIRED" on the result screen without a second
        # round trip.
        run["dropped_item_key"] = dropped_item.key if dropped_item else None
        return run

    if _conn is not None:
        return await _run(_conn)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _run(conn)


async def _grant_cash(conn, guild_id: int, clone_id: Optional[int], user_id: int, amount: int, run_id: int) -> None:
    """Credits the shared economy currency (discord_economy_balances) using
    an atomic increment rather than read-then-write, so this can never lose
    a concurrent update. Tagged with the run_id for audit/reconciliation."""
    await conn.execute(
        """
        INSERT INTO discord_economy_balances (guild_id, clone_id, user_id, balance)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET
            balance = discord_economy_balances.balance + EXCLUDED.balance
        """,
        guild_id, clone_id, user_id, amount,
    )
    await conn.execute(
        """
        INSERT INTO discord_economy_transactions (guild_id, clone_id, user_id, amount, reason)
        VALUES ($1, $2, $3, $4, $5)
        """,
        guild_id, clone_id, user_id, amount, f"heist_wars:run:{run_id}",
    )


# --- progression -----------------------------------------------------------

XP_PER_LEVEL = 100  # simple, predictable v1 formula (brief §15): flat cost per level
MAX_LEVEL = 50


async def _apply_level_ups(conn, guild_id: int, clone_id: Optional[int], user_id: int) -> None:
    row = await conn.fetchrow(
        """
        SELECT level, xp FROM heist_players
        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
        FOR UPDATE
        """,
        guild_id, clone_id, user_id,
    )
    if not row:
        return
    level, xp = row["level"], row["xp"]
    new_level = min(MAX_LEVEL, 1 + xp // XP_PER_LEVEL)
    # XP overflow: XP is never reset/consumed on level-up (cumulative-XP
    # model, matching modules/leveling.py's convention) — level is always
    # re-derived from total XP, so no XP is ever "lost" past a level's
    # threshold. Above MAX_LEVEL, XP still accrues but level is capped.
    if new_level != level:
        await conn.execute(
            "UPDATE heist_players SET level = $4 WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3",
            guild_id, clone_id, user_id, new_level,
        )


# --- expiration / resume ----------------------------------------------------

async def _expire_abandoned_run(conn, guild_id: int, clone_id: Optional[int], user_id: int) -> None:
    cutoff = datetime.now(timezone.utc) - ABANDONED_RUN_TIMEOUT
    row = await conn.fetchrow(
        """
        UPDATE heist_runs SET status = 'EXPIRED', updated_at = NOW()
        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
          AND status NOT IN ('COMPLETED', 'FAILED', 'EXPIRED')
          AND updated_at < $4
        RETURNING id
        """,
        guild_id, clone_id, user_id, cutoff,
    )
    if row:
        await _log_event(conn, row["id"], "heist_expired")


async def resume_heist(guild_id: int, clone_id: Optional[int], user_id: int) -> Optional[dict]:
    """Returns the player's active run (restoring state after a bot
    restart / reconnect), expiring it first if it's stale."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _expire_abandoned_run(conn, guild_id, clone_id, user_id)
            row = await conn.fetchrow(
                """
                SELECT * FROM heist_runs
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
                  AND status NOT IN ('COMPLETED', 'FAILED', 'EXPIRED')
                """,
                guild_id, clone_id, user_id,
            )
            return dict(row) if row else None


async def current_event_for_run(run: dict) -> Optional[dict]:
    """Looks up the event currently pending for a run's phase (for
    re-rendering the UI after a resume). Returns None for non-event phases."""
    phase = HeistState(run["status"])
    if phase not in EVENT_PHASES:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT event_key FROM heist_events_log
            WHERE run_id = $1 AND action = 'event_presented' AND phase = $2
            ORDER BY id DESC LIMIT 1
            """,
            run["id"], phase.value,
        )
    if not row:
        return None
    from game.locations import get_event
    event = get_event(row["event_key"])
    return {"phase": phase, "event": event}
