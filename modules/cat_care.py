# path: modules/cat_care.py

"""
Cat care (virtual only: play coins, never real money; see modules/cat_wallet.py).

  * Water (30 coins, lasts 24h) and a bed (120 coins, 24h of rent) keep a cat comfortable: each halves its chance of
    getting hungry.
  * Hunger is random. A cat that gets hungry stays hungry until someone buys it food (80 coins).
  * A cat that is still hungry after 48 hours wanders off. It is not lost: it comes back in a later spawn in the
    server it was adopted in (same name, rarity and personality), and anyone can adopt it again.
  * The owner gets one gentle heads-up at 24 hours, and a kind note when the cat leaves. Wording is sad, never hurtful.
  * A cat that is for sale on the market is never hungry and never leaves (it is in the shop window).

Pure rules at the top (no database), queries below. Every spend is ONE transaction with the cat row locked, so two quick
taps can never spend coins twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from modules import cat_wallet

logger = logging.getLogger(__name__)

WATER_COST = 30
BED_COST = 120
FOOD_COST = 80
COMFORT_HOURS = 24                  # one purchase of water or of bed rent lasts this long
RENEW_WINDOW_HOURS = 12             # can be topped up when less than this is left (no stacking weeks ahead)
HUNGER_TICK_SECONDS = 1800          # how often the care loop looks at cats
HUNGER_CHANCE = 1 / 48              # per tick: on average one hunger a day for a cat with no comfort
COMFORT_FACTOR = 0.5                # each of water / bed halves it
GRACE_HOURS = 12                    # a new or just-fed cat can't be hungry yet
WARN_AFTER_HOURS = 24
LEAVE_AFTER_HOURS = 48
RUNAWAY_ANYWHERE_DAYS = 3           # if its own server stopped spawning cats, it returns anywhere after this long
BATCH = 500
BULK_LIMIT = 100                    # most cats one 'all' tap looks after

DROP, BED, FOOD = "💧", "🛏️", "🍖"


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def is_hungry(cat) -> bool:
    return getattr(cat, "hungry_since", None) is not None


def leaves_at(cat) -> Optional[datetime]:
    since = _utc(getattr(cat, "hungry_since", None))
    return since + timedelta(hours=LEAVE_AFTER_HOURS) if since else None


def active(until: Optional[datetime], now: Optional[datetime] = None) -> bool:
    until = _utc(until)
    return until is not None and until > _now(now)


def hunger_chance(bed: bool, water: bool) -> float:
    """The chance a cat gets hungry in one tick."""
    return HUNGER_CHANCE * (COMFORT_FACTOR if bed else 1.0) * (COMFORT_FACTOR if water else 1.0)


def status_lines(cat, now: Optional[datetime] = None) -> str:
    """The care part of a cat's card. Discord timestamps show each viewer's own time."""
    now = _now(now)
    out = []
    if is_hungry(cat):
        ts = int(leaves_at(cat).timestamp())
        out.append(f"🍽️ **Hungry**, it would love some food. If nobody feeds it, it may wander off <t:{ts}:R>.")
    else:
        out.append("😸 **Happy and fed**")
    out.append(f"{DROP} Water: " + (f"until <t:{int(_utc(cat.watered_until).timestamp())}:R>" if active(cat.watered_until, now)
                                    else "none right now"))
    out.append(f"{BED} Bed: " + (f"rented until <t:{int(_utc(cat.bed_until).timestamp())}:R>" if active(cat.bed_until, now)
                                 else "none right now"))
    return "\n".join(out)


def runaway_owner_text(names: List[str]) -> str:
    if len(names) == 1:
        return (f"🐾 **{names[0]}** wandered off today. It was hungry for a while and it missed having someone around. "
                "It's safe and will turn up in a server spawn again, so someone may give it a new home. "
                "You can adopt more cats any time. Next time, a little food keeps a cat close to home. 💛")
    return (f"🐾 {len(names)} of your cats wandered off ({', '.join(names[:5])}). They were hungry for a while and missed "
            "having someone around. They are safe and will turn up in server spawns again. "
            "A little food keeps a cat close to home. 💛")


def warning_text(names: List[str]) -> str:
    shown = ", ".join(f"**{n}**" for n in names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return (f"🍽️ {shown} {'is' if len(names) == 1 else 'are'} getting hungry. Open **My cats** and tap {FOOD} **Feed** "
            f"({FOOD_COST} coins) whenever you can. A cat that stays hungry for a couple of days may wander off.")


def returned_embed_text(name: str) -> str:
    return (f"🐾 **{name}** is back! It wandered off from a busy home and is looking for someone with a little more "
            "time. Could that be you? Tap **Adopt me!** 💛")


# ── database ──────────────────────────────────────────────────────────────

async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


@dataclass(frozen=True)
class CareResult:
    status: str                 # ok | poor | gone | listed | not_hungry | already
    coins: int = 0              # coins left after a successful spend, or the coins the person has on "poor"
    cost: int = 0
    until: Optional[datetime] = None


_NOT_FOR_SALE = "NOT EXISTS (SELECT 1 FROM cat_listings l WHERE l.adoption_id = a.id AND l.status = 'open')"


async def _spend(conn, owner_id: int, clone_id, cost: int) -> Optional[int]:
    """Take `cost` coins in one statement. The coins left, or None when there aren't enough."""
    row = await conn.fetchrow(
        "UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
        "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
        owner_id, -1, int(cost))                  # -1: the wallet is shared by every bot
    return int(row["coins"]) if row else None


async def _lock_cat(conn, cat_id: int, owner_id: int, clone_id):
    return await conn.fetchrow(
        "SELECT a.*, " + ("EXISTS (SELECT 1 FROM cat_listings l WHERE l.adoption_id = a.id AND l.status = 'open')") +
        " AS for_sale FROM cat_adoptions a WHERE a.id = $1 AND a.owner_id = $2 AND COALESCE(a.clone_id, -1) = $3 FOR UPDATE",
        cat_id, owner_id, _key(clone_id))


async def feed(cat_id: int, owner_id: int, clone_id) -> CareResult:
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat = await _lock_cat(conn, cat_id, owner_id, clone_id)
            if cat is None:
                return CareResult("gone")
            if cat["hungry_since"] is None:
                return CareResult("not_hungry", cost=FOOD_COST)
            left = await _spend(conn, owner_id, clone_id, FOOD_COST)
            if left is None:
                return CareResult("poor", coins=(await cat_wallet.get_wallet(owner_id, clone_id)).coins, cost=FOOD_COST)
            await conn.execute("UPDATE cat_adoptions SET hungry_since = NULL, hunger_warned = NULL, fed_at = NOW() "
                               "WHERE id = $1", cat_id)
            return CareResult("ok", coins=left, cost=FOOD_COST)


async def _comfort(cat_id: int, owner_id: int, clone_id, column: str, cost: int) -> CareResult:
    assert column in ("watered_until", "bed_until")
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat = await _lock_cat(conn, cat_id, owner_id, clone_id)
            if cat is None:
                return CareResult("gone")
            now = datetime.now(timezone.utc)
            have = _utc(cat[column])
            if have is not None and have > now + timedelta(hours=RENEW_WINDOW_HOURS):
                return CareResult("already", cost=cost, until=have)
            left = await _spend(conn, owner_id, clone_id, cost)
            if left is None:
                return CareResult("poor", coins=(await cat_wallet.get_wallet(owner_id, clone_id)).coins, cost=cost)
            until = max(now, have or now) + timedelta(hours=COMFORT_HOURS)
            await conn.execute(f"UPDATE cat_adoptions SET {column} = $2 WHERE id = $1", cat_id, until)
            return CareResult("ok", coins=left, cost=cost, until=until)


async def water(cat_id: int, owner_id: int, clone_id) -> CareResult:
    return await _comfort(cat_id, owner_id, clone_id, "watered_until", WATER_COST)


async def rent_bed(cat_id: int, owner_id: int, clone_id) -> CareResult:
    return await _comfort(cat_id, owner_id, clone_id, "bed_until", BED_COST)


# ── "all" buttons: the same single-cat actions (same prices, same rules), run for every cat that needs it ──

BULK_KINDS = {"feed": ("hungry_since", FOOD_COST), "water": ("watered_until", WATER_COST), "bed": ("bed_until", BED_COST)}


@dataclass(frozen=True)
class BulkResult:
    kind: str
    done: int = 0               # cats looked after
    spent: int = 0
    coins: Optional[int] = None  # coins left after the last spend (or what the person has when they ran short)
    short: int = 0              # cats that still needed it when the coins ran out
    skipped: int = 0            # cats that didn't need it any more by the time their turn came


def bulk_target_count(cats, kind: str, now: Optional[datetime] = None) -> int:
    """How many of `cats` an 'all' tap would look after (for the button state; the database has the last word)."""
    now = _now(now)
    if kind == "feed":
        return sum(1 for c in cats if is_hungry(c))
    col = BULK_KINDS[kind][0]
    return sum(1 for c in cats if not active(getattr(c, col, None), now + timedelta(hours=RENEW_WINDOW_HOURS)))


async def _candidates(owner_id: int, clone_id, kind: str) -> List[int]:
    """Ids of this person's cats that need `kind`, most urgent first. Cats for sale on the market are left alone."""
    col = BULK_KINDS[kind][0]            # fixed names only, never user input
    pool = await _pool()
    async with pool.acquire() as conn:
        if kind == "feed":
            rows = await conn.fetch(
                "SELECT a.id FROM cat_adoptions a WHERE a.owner_id = $1 AND COALESCE(a.clone_id, -1) = $2 "
                f"AND a.hungry_since IS NOT NULL AND {_NOT_FOR_SALE} ORDER BY a.hungry_since, a.id LIMIT $3",
                owner_id, _key(clone_id), BULK_LIMIT)
        else:
            edge = datetime.now(timezone.utc) + timedelta(hours=RENEW_WINDOW_HOURS)
            rows = await conn.fetch(
                "SELECT a.id FROM cat_adoptions a WHERE a.owner_id = $1 AND COALESCE(a.clone_id, -1) = $2 "
                f"AND (a.{col} IS NULL OR a.{col} <= $4) AND {_NOT_FOR_SALE} ORDER BY a.id LIMIT $3",
                owner_id, _key(clone_id), BULK_LIMIT, edge)
    return [int(r["id"]) for r in rows]


async def bulk_preview(owner_id: int, clone_id, kind: str):
    """(cats, coins) an 'all' tap would look after and cost right now."""
    ids = await _candidates(owner_id, clone_id, kind)
    return len(ids), len(ids) * BULK_KINDS[kind][1]


async def bulk_care(owner_id: int, clone_id, kind: str) -> BulkResult:
    """Feed / water / rent a bed for every cat that needs it. Each cat is its own safe single action, so a tap that runs
    out of coins part-way keeps what it already did and says how many are left."""
    action = {"feed": feed, "water": water, "bed": rent_bed}[kind]
    ids = await _candidates(owner_id, clone_id, kind)
    done = spent = skipped = 0
    coins: Optional[int] = None
    for n, cat_id in enumerate(ids):
        try:
            res = await action(cat_id, owner_id, clone_id)
        except Exception:
            logger.exception("[cat-care] bulk %s failed for cat %s", kind, cat_id)
            skipped += 1
            continue
        if res.status == "ok":
            done += 1
            spent += res.cost
            coins = res.coins
        elif res.status == "poor":
            return BulkResult(kind, done, spent, res.coins, short=len(ids) - n, skipped=skipped)
        else:
            skipped += 1
    return BulkResult(kind, done, spent, coins, skipped=skipped)


async def get_cat_care(cat_id: int):
    """(hungry_since, watered_until, bed_until) of one cat, fresh from the database, or None."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT hungry_since, watered_until, bed_until FROM cat_adoptions WHERE id = $1", cat_id)
    return (row["hungry_since"], row["watered_until"], row["bed_until"]) if row else None


# ── the care loop (one tick) ──────────────────────────────────────────────

async def make_hungry(clone_id) -> int:
    """Random hunger for this bot's cats. One statement; returns how many cats just got hungry."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE cat_adoptions SET hungry_since = NOW(), hunger_warned = NULL WHERE id IN ("
            " SELECT a.id FROM cat_adoptions a WHERE COALESCE(a.clone_id, -1) = $1 AND a.hungry_since IS NULL "
            " AND a.adopted_at < NOW() - make_interval(hours => $2) "
            " AND (a.fed_at IS NULL OR a.fed_at < NOW() - make_interval(hours => $2)) "
            f" AND {_NOT_FOR_SALE} "
            " AND random() < $3::float8 * (CASE WHEN a.bed_until > NOW() THEN $4::float8 ELSE 1 END) "
            "     * (CASE WHEN a.watered_until > NOW() THEN $4::float8 ELSE 1 END) "
            " LIMIT $5) RETURNING id",
            _key(clone_id), GRACE_HOURS, HUNGER_CHANCE, COMFORT_FACTOR, BATCH)
    return len(rows)


async def due_warnings(clone_id) -> dict:
    """{owner_id: [cat names]} of cats hungry for 24h+ whose owner wasn't told yet. Marks them as told."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE cat_adoptions SET hunger_warned = NOW() WHERE id IN ("
            " SELECT id FROM cat_adoptions WHERE COALESCE(clone_id, -1) = $1 AND hungry_since IS NOT NULL "
            " AND hunger_warned IS NULL AND hungry_since <= NOW() - make_interval(hours => $2) LIMIT $3) "
            "RETURNING owner_id, name", _key(clone_id), WARN_AFTER_HOURS, BATCH)
    out: dict = {}
    for r in rows:
        out.setdefault(int(r["owner_id"]), []).append(r["name"])
    return out


async def make_runaways(clone_id) -> List[dict]:
    """Cats hungry for 48h+ leave home. Each is removed from its owner and queued in cat_runaways, in ONE transaction.
    Returns the facts for the log and the owners' notes."""
    pool = await _pool()
    left: List[dict] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT a.* FROM cat_adoptions a WHERE COALESCE(a.clone_id, -1) = $1 AND a.hungry_since IS NOT NULL "
                f"AND a.hungry_since <= NOW() - make_interval(hours => $2) AND {_NOT_FOR_SALE} "
                "ORDER BY a.hungry_since LIMIT $3 FOR UPDATE", _key(clone_id), LEAVE_AFTER_HOURS, BATCH)
            for r in rows:
                await conn.execute("DELETE FROM cat_adoptions WHERE id = $1", r["id"])
                await conn.execute(
                    "INSERT INTO cat_runaways (pool_id, guild_id, cat_name, rarity, trait) VALUES ($1,$2,$3,$4,$5) "
                    "ON CONFLICT (pool_id) DO UPDATE SET guild_id = EXCLUDED.guild_id, cat_name = EXCLUDED.cat_name, "
                    "rarity = EXCLUDED.rarity, trait = EXCLUDED.trait, left_at = NOW(), returned_at = NULL, claimed_at = NULL",
                    r["pool_id"], r["adopted_guild_id"], r["name"], r["rarity"], r["trait"])
                left.append(dict(owner_id=int(r["owner_id"]), pool_id=int(r["pool_id"]), cat_name=r["name"],
                                 rarity=r["rarity"], trait=r["trait"], guild_id=r["adopted_guild_id"]))
    return left


@dataclass(frozen=True)
class Runaway:
    pool_id: int
    cat_name: str
    rarity: str
    trait: str
    guild_id: Optional[int]


async def take_runaway(guild_id: int) -> Optional[Runaway]:
    """One cat that left home and may come back in a spawn of THIS server (its own server, or any server once it has
    waited RUNAWAY_ANYWHERE_DAYS). Marked as returned so two servers can't both post it."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE cat_runaways SET returned_at = NOW() WHERE pool_id = ("
            " SELECT pool_id FROM cat_runaways WHERE returned_at IS NULL AND claimed_at IS NULL "
            " AND (guild_id = $1 OR guild_id IS NULL OR left_at < NOW() - make_interval(days => $2)) "
            " ORDER BY left_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING pool_id, cat_name, rarity, trait, guild_id", guild_id, RUNAWAY_ANYWHERE_DAYS)
    return Runaway(int(row["pool_id"]), row["cat_name"], row["rarity"], row["trait"], row["guild_id"]) if row else None


async def undo_return(pool_id: int) -> None:
    """The returned cat could not be posted: put it back in the queue so it isn't lost."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE cat_runaways SET returned_at = NULL WHERE pool_id = $1 AND claimed_at IS NULL", pool_id)
    except Exception:
        logger.warning("[cat-care] couldn't requeue runaway cat %s", pool_id, exc_info=True)
