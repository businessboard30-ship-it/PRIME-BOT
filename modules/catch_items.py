"""Player inventory, starter kit and daily reward (Phase 2/4 slice).

* ``grant_items`` / ``load_inventory`` are the only code paths that add or read
  ``catch_inventory`` rows (throws consume them inside ``record_catch``).
* ``ensure_starter_kit`` gives every player a one-time stack of basic capsules the first
  time they interact, so a first throw can never fail for lack of balls. The "already
  given" flag lives on ``catch_players.settings`` and is flipped in the same statement
  that decides to grant, so a double click grants once.
* ``claim_daily`` runs in one transaction with the player row locked, so two taps cannot
  both pay out. Reward numbers are balance placeholders (tuned in P11-06).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from modules import catch_db
from modules.catch_cooldowns import DEFAULTS as COOLDOWN_DEFAULTS
from modules.catch_game import BAIT_BONUS, BALLS

STARTER_KIT: dict[str, int] = {"capsule_basic": 10}
DAILY_COOLDOWN_SECONDS = COOLDOWN_DEFAULTS["daily"]
DAILY_STREAK_WINDOW = timedelta(hours=48)
ITEM_NAMES = {
    "capsule_basic": "Basic capsule",
    "capsule_sturdy": "Sturdy capsule",
    "capsule_prime": "Prime capsule",
    "capsule_sovereign": "Sovereign capsule",
    "honeyberry": "Honeyberry",
    "goldberry": "Goldberry",
}
KNOWN_ITEMS = frozenset(ITEM_NAMES)


@dataclass(frozen=True, slots=True)
class DailyReward:
    coins: int
    items: dict[str, int]


@dataclass(frozen=True, slots=True)
class DailyResult:
    claimed: bool
    streak: int
    ready_at: datetime
    reward: DailyReward | None = None


@dataclass(frozen=True, slots=True)
class PlayerSummary:
    coins: int = 0
    total_catches: int = 0
    catch_streak: int = 0
    daily_streak: int = 0
    last_daily_at: datetime | None = None


def daily_reward(streak: int) -> DailyReward:
    """Reward for claiming on a given streak day (1-based)."""
    streak = max(1, int(streak))
    items = {"capsule_basic": 5}
    if streak % 7 == 0:
        items["capsule_sturdy"] = 2
    if streak % 30 == 0:
        items["capsule_prime"] = 1
    return DailyReward(coins=100 + 25 * min(streak - 1, 6), items=items)


def next_streak(previous: int, last_claim: datetime | None, now: datetime) -> int:
    """Streak after a claim at ``now``: continues within 48h of the last claim, else restarts."""
    if last_claim is not None and now - last_claim <= DAILY_STREAK_WINDOW:
        return int(previous) + 1
    return 1


def item_name(key: str) -> str:
    return ITEM_NAMES.get(key, key.replace("_", " ").title())


async def grant_items(user_id: int, clone_id: int | None, items: dict[str, int], *, conn=None) -> None:
    for key, quantity in items.items():
        if key not in KNOWN_ITEMS:
            raise KeyError(f"unknown item: {key}")
        if quantity <= 0:
            raise ValueError("item quantity must be positive")
    async with catch_db.transaction(conn) as c:
        for key, quantity in items.items():
            await c.execute(
                "INSERT INTO catch_inventory (user_id, clone_id, item_key, quantity) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (user_id, clone_key, item_key) DO UPDATE SET "
                "quantity = catch_inventory.quantity + EXCLUDED.quantity, updated_at = now()",
                user_id, clone_id, key, quantity,
            )


async def load_inventory(user_id: int, clone_id: int | None, *, conn=None) -> dict[str, int]:
    async with catch_db.connection(conn) as c:
        rows = await c.fetch(
            "SELECT item_key, quantity FROM catch_inventory WHERE user_id=$1 AND clone_key=$2 AND quantity > 0",
            user_id, catch_db.clone_key(clone_id),
        )
    return {str(r["item_key"]): int(r["quantity"]) for r in rows}


async def load_player(user_id: int, clone_id: int | None, *, conn=None) -> PlayerSummary:
    async with catch_db.connection(conn) as c:
        row = await c.fetchrow(
            "SELECT coins, total_catches, catch_streak, daily_streak, last_daily_at "
            "FROM catch_players WHERE user_id=$1 AND clone_key=$2",
            user_id, catch_db.clone_key(clone_id),
        )
    if row is None:
        return PlayerSummary()
    return PlayerSummary(int(row["coins"]), int(row["total_catches"]), int(row["catch_streak"]),
                         int(row["daily_streak"]), row["last_daily_at"])


async def ensure_starter_kit(user_id: int, clone_id: int | None, *, conn=None) -> bool:
    """Grant the starter kit once per player. Returns True only on the call that granted it."""
    async with catch_db.transaction(conn) as c:
        flipped = await c.fetchval(
            "INSERT INTO catch_players (user_id, clone_id, settings) VALUES ($1, $2, '{\"starter_kit\": true}'::jsonb) "
            "ON CONFLICT (user_id, clone_key) DO UPDATE SET "
            "settings = catch_players.settings || '{\"starter_kit\": true}'::jsonb, updated_at = now() "
            "WHERE NOT COALESCE((catch_players.settings->>'starter_kit')::boolean, FALSE) "
            "RETURNING 1",
            user_id, clone_id,
        )
        if flipped is None:
            return False
        await grant_items(user_id, clone_id, STARTER_KIT, conn=c)
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, action, detail) "
            "VALUES ($1, $1, $2, 'starter_kit', $3::jsonb)",
            user_id, clone_id, '{"capsule_basic": %d}' % STARTER_KIT["capsule_basic"],
        )
    return True


async def claim_daily(user_id: int, clone_id: int | None, *, conn=None) -> DailyResult:
    """Pay out the daily reward if it is ready; otherwise report when it will be."""
    async with catch_db.transaction(conn) as c:
        await c.execute(
            "INSERT INTO catch_players (user_id, clone_id) VALUES ($1, $2) ON CONFLICT (user_id, clone_key) DO NOTHING",
            user_id, clone_id,
        )
        row = await c.fetchrow(
            "SELECT daily_streak, last_daily_at, now() AS now FROM catch_players "
            "WHERE user_id=$1 AND clone_key=$2 FOR UPDATE",
            user_id, catch_db.clone_key(clone_id),
        )
        now: datetime = row["now"] if row["now"].tzinfo else row["now"].replace(tzinfo=timezone.utc)
        last: datetime | None = row["last_daily_at"]
        if last is not None:
            ready_at = last + timedelta(seconds=DAILY_COOLDOWN_SECONDS)
            if now < ready_at:
                return DailyResult(False, int(row["daily_streak"]), ready_at)
        streak = next_streak(int(row["daily_streak"]), last, now)
        reward = daily_reward(streak)
        await c.execute(
            "UPDATE catch_players SET daily_streak=$3, last_daily_at=now(), coins=coins+$4, updated_at=now() "
            "WHERE user_id=$1 AND clone_key=$2",
            user_id, catch_db.clone_key(clone_id), streak, reward.coins,
        )
        await grant_items(user_id, clone_id, reward.items, conn=c)
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, action, detail) "
            "VALUES ($1, $1, $2, 'daily', jsonb_build_object('streak', $3::int, 'coins', $4::int))",
            user_id, clone_id, streak, reward.coins,
        )
    return DailyResult(True, streak, now + timedelta(seconds=DAILY_COOLDOWN_SECONDS), reward)


def sorted_inventory(inventory: dict[str, int]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(balls, berries) in stable game order, hiding empty stacks."""
    balls = [(k, inventory[k]) for k in BALLS if inventory.get(k, 0) > 0]
    berries = [(k, inventory[k]) for k in BAIT_BONUS if inventory.get(k, 0) > 0]
    return balls, berries


__all__ = [
    "DAILY_COOLDOWN_SECONDS", "DailyResult", "DailyReward", "ITEM_NAMES", "PlayerSummary", "STARTER_KIT",
    "claim_daily", "daily_reward", "ensure_starter_kit", "grant_items", "item_name", "load_inventory",
    "load_player", "next_streak", "sorted_inventory",
]
