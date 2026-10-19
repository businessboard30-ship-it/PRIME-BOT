# path: modules/cat_life.py

"""
Cat Life (play coins only, never real money; see modules/cat_wallet.py).

  * Earning cats: every cat that is fed earns coins by the hour (up to HOURS_CAP hours between pickups). Rarer and higher
    ranked cats earn more. Pick the coins up with "Collect".
  * Home rent: a cozy home is rented by the day. Cats earn full coins while it is paid. When the rent runs out nothing bad
    happens to the cats, they just earn less (HOME_OFF_FACTOR) until it is paid again.
  * Walks: a cat goes out for a few hours. It costs coins, and it brings back a different amount (often a bit more, sometimes less).
  * Heists: a cat and its owner go on a pretend cartoon heist on an imaginary place. Win: a bigger payout. Fail: the fee is lost.
    It is always against pretend targets, never against another player, and nothing here can hurt or lose a cat (fights are separate: see cat_arena, and always need an accept).
  * Lucky games: coin flip and lucky spin. The house edge is built in and a daily loss limit protects players.

Pure rules at the top (no database), queries below. Every spend is ONE transaction with the rows locked, so two quick taps
can never spend coins twice.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from modules import cat_wallet

logger = logging.getLogger(__name__)

COINS, DIAMOND = cat_wallet.COINS, cat_wallet.DIAMOND
_RNG = random.SystemRandom()

# ── earning cats ──────────────────────────────────────────────────────────
HOURS_CAP = 12                                   # coins stop piling up after this many hours: come back and collect
EARN_PER_HOUR = {"common": 10, "uncommon": 15, "rare": 25, "legendary": 50}
RANK_BONUS = 0.10                                # +10% per rank
HOME_OFF_FACTOR = 0.25                           # earnings while the rent is not paid

# ── home rent ─────────────────────────────────────────────────────────────
RENT_BASE = 200                                  # per day
RENT_PER_CAT = 50                                # per day, per cat
RENT_CAT_CAP = 20
RENT_MAX_PREPAID_DAYS = 3

# ── walks ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Walk:
    key: str
    emoji: str
    name: str
    hours: int
    fee: int
    low: float                                   # payout is fee x a random number between low and high (before bonuses)
    high: float
    diamond_chance: float = 0.0


WALKS: Dict[str, Walk] = {w.key: w for w in (
    Walk("garden", "🌷", "Flower garden stroll", 1, 100, 0.60, 1.40),
    Walk("park", "🌳", "Sunny park picnic", 3, 300, 0.60, 1.40),
    Walk("beach", "🏖️", "Beach & shell hunt", 6, 700, 0.60, 1.50),
    Walk("moon", "🌙", "Moonlight rooftop walk", 12, 1500, 0.50, 1.60, diamond_chance=0.01),
)}
WALK_RARITY_BONUS = {"common": 1.0, "uncommon": 1.05, "rare": 1.10, "legendary": 1.20}
WALK_RANK_BONUS = 0.05

# ── heists ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Heist:
    key: str
    emoji: str
    name: str
    seconds: int                                 # how long the heist takes: the result comes in seconds
    fee: int
    success: float                               # base chance
    multiplier: float                            # total returned on a win (the fee is included)


HEISTS: Dict[str, Heist] = {h.key: h for h in (
    Heist("bakery", "🧁", "Sweet Strawberry Bakery", 8, 200, 0.70, 1.35),
    Heist("jewel", "💍", "Sparkle Jewel Shop", 12, 600, 0.50, 1.90),
    Heist("bank", "🏦", "Grand Pink Bank", 18, 1500, 0.30, 3.10),
    Heist("vault", "👑", "Royal Treasure Vault", 25, 4000, 0.10, 9.00),
)}
HEIST_RARITY_BONUS = {"common": 0.0, "uncommon": 0.02, "rare": 0.04, "legendary": 0.07}
HEIST_RANK_BONUS = 0.01                          # per rank (rank is 0-5)
HEIST_MAX_SUCCESS = 0.85
HEISTS_PER_DAY = 6

# ── lucky games ───────────────────────────────────────────────────────────
BETS = (100, 500, 1000, 2500, 5000)
FLIP_WIN_CHANCE = 0.47
FLIP_MULTIPLIER = 2
SPIN = ((0.0, 42), (0.5, 26), (1.0, 14), (2.0, 11), (5.0, 6), (15.0, 1))    # (multiplier of the bet, weight)
DAILY_LOSS_LIMIT = 20_000                        # coins; after that the games rest until UTC midnight


# ── pure rules ────────────────────────────────────────────────────────────

def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def today() -> date:
    return datetime.now(timezone.utc).date()


def hourly_rate(rarity: str, rank: int) -> float:
    return EARN_PER_HOUR.get(rarity, EARN_PER_HOUR["common"]) * (1 + RANK_BONUS * max(0, min(5, int(rank or 0))))


def overlap_hours(a0: datetime, a1: datetime, b0: Optional[datetime], b1: Optional[datetime]) -> float:
    """Hours the span [a0, a1] shares with [b0, b1]."""
    if b0 is None or b1 is None:
        return 0.0
    lo, hi = max(_utc(a0), _utc(b0)), min(_utc(a1), _utc(b1))
    return max(0.0, (hi - lo).total_seconds() / 3600)


@dataclass(frozen=True)
class EarnCat:
    rarity: str
    rank: int
    since: datetime                              # when it started earning for this pickup (last pickup or adoption)
    hungry: bool = False


def earnings(cats: Sequence[EarnCat], now: datetime, home_since: Optional[datetime], home_until: Optional[datetime]) -> int:
    """Coins waiting. A hungry cat earns nothing; hours are capped; rent-covered hours pay in full, the rest at HOME_OFF_FACTOR."""
    total = 0.0
    for c in cats:
        if c.hungry:
            continue
        start = max(_utc(c.since), _utc(now) - timedelta(hours=HOURS_CAP))
        hours = max(0.0, (_utc(now) - start).total_seconds() / 3600)
        paid = min(hours, overlap_hours(start, now, home_since, home_until))
        total += hourly_rate(c.rarity, c.rank) * (paid + (hours - paid) * HOME_OFF_FACTOR)
    return int(total)


def earn_since(collected: Optional[datetime], adopted: Optional[datetime], now: datetime) -> datetime:
    """When a cat starts earning for the next pickup: the later of the last pickup and its adoption."""
    marks = [_utc(x) for x in (collected, adopted) if x is not None]
    return max(marks) if marks else _utc(now)


def rent_cost(cat_count: int, days: int = 1) -> int:
    return (RENT_BASE + RENT_PER_CAT * min(max(0, cat_count), RENT_CAT_CAP)) * max(1, days)


def daily_earning_estimate(cats: Sequence[EarnCat]) -> int:
    """What the fed cats earn in a day when picked up twice a day with the rent paid (for the rent screen)."""
    return int(sum(hourly_rate(c.rarity, c.rank) * 24 for c in cats if not c.hungry))


def walk_payout(walk: Walk, rarity: str, rank: int, rng=_RNG) -> Tuple[int, int]:
    """(coins, diamonds) a walk brings back."""
    bonus = WALK_RARITY_BONUS.get(rarity, 1.0) * (1 + WALK_RANK_BONUS * max(0, min(5, int(rank or 0))))
    coins = int(round(walk.fee * rng.uniform(walk.low, walk.high) * bonus))
    diamonds = 1 if walk.diamond_chance and rng.random() < walk.diamond_chance else 0
    return max(0, coins), diamonds


def heist_chance(heist: Heist, rarity: str, rank: int) -> float:
    bonus = HEIST_RARITY_BONUS.get(rarity, 0.0) + HEIST_RANK_BONUS * max(0, min(5, int(rank or 0)))
    return min(HEIST_MAX_SUCCESS, heist.success + bonus)


def heist_roll(heist: Heist, rarity: str, rank: int, rng=_RNG) -> Tuple[bool, int]:
    """(success, payout). A failed heist pays nothing: the fee is gone."""
    if rng.random() < heist_chance(heist, rarity, rank):
        return True, int(round(heist.fee * heist.multiplier))
    return False, 0


def flip_roll(rng=_RNG) -> bool:
    return rng.random() < FLIP_WIN_CHANCE


def spin_roll(rng=_RNG) -> float:
    return rng.choices([m for m, _ in SPIN], weights=[w for _, w in SPIN], k=1)[0]


def spin_expected_multiplier() -> float:
    return sum(m * w for m, w in SPIN) / sum(w for _, w in SPIN)


def percent(p: float) -> str:
    return f"{round(p * 100)}%"


WALK_STORIES = (
    "{cat} sniffed every flower, chased a butterfly 🦋 and found {coins} on the way home.",
    "{cat} pranced along in a tiny bow 🎀 and a kind stranger tucked {coins} into the basket.",
    "{cat} napped in the sunshine ☀️, then trotted back with {coins}.",
    "{cat} made friends with a puppy 🐶 and came home with {coins} and a happy purr.",
)
WALK_SAD_STORIES = (
    "{cat} got distracted by a fluffy cloud ☁️ and only found {coins} this time.",
    "{cat} met a very chatty pigeon 🕊️ and came home a little late with {coins}.",
)
HEIST_WIN_STORIES = (
    "You and {cat} tiptoed in wearing little sparkly masks 🎀 and slipped out with the goodies! +{coins}",
    "{cat} distracted the guard with a ball of yarn 🧶 while you grabbed the loot! +{coins}",
)
HEIST_FAIL_STORIES = (
    "A grumpy guard dog 🐶 spotted {cat}, and you paid the cute little fine. Better luck next time! 💖",
    "{cat} sneezed at the worst moment 🤧 and the plan fell apart. You paid the fee and ran home for cuddles. 💖",
)


def walk_story(cat_name: str, coins: int, fee: int, rng=_RNG) -> str:
    pool = WALK_STORIES if coins >= fee else WALK_SAD_STORIES
    return rng.choice(pool).format(cat=cat_name, coins=f"{COINS} {coins:,}")


def heist_story(cat_name: str, success: bool, payout: int, rng=_RNG) -> str:
    pool = HEIST_WIN_STORIES if success else HEIST_FAIL_STORIES
    return rng.choice(pool).format(cat=cat_name, coins=f"{COINS} {payout:,}")


# ── database ──────────────────────────────────────────────────────────────

async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _spend(conn, owner_id: int, cost: int) -> Optional[int]:
    """Take `cost` coins in one statement. The coins left, or None when there aren't enough. (-1: one shared wallet.)"""
    row = await conn.fetchrow(
        "UPDATE cat_wallets SET coins = coins - $2, updated_at = NOW() "
        "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = -1 AND coins >= $2 RETURNING coins", owner_id, int(cost))
    return int(row["coins"]) if row else None


async def _pay(conn, owner_id: int, coins: int = 0, diamonds: int = 0) -> None:
    await conn.execute(
        "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1, NULL, $2, $3) "
        "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
        "coins = cat_wallets.coins + EXCLUDED.coins, diamonds = cat_wallets.diamonds + EXCLUDED.diamonds, "
        "updated_at = NOW()", owner_id, int(coins), int(diamonds))


async def _state(conn, owner_id: int, clone_id):
    await conn.execute("INSERT INTO cat_life_state (owner_id, clone_key) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                       owner_id, _key(clone_id))
    return await conn.fetchrow("SELECT * FROM cat_life_state WHERE owner_id = $1 AND clone_key = $2 FOR UPDATE",
                               owner_id, _key(clone_id))


async def _daily(conn, owner_id: int):
    await conn.execute("INSERT INTO cat_life_daily (owner_id, day) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                       owner_id, today())
    return await conn.fetchrow("SELECT * FROM cat_life_daily WHERE owner_id = $1 AND day = $2 FOR UPDATE",
                               owner_id, today())


_FREE_CAT = ("NOT EXISTS (SELECT 1 FROM cat_listings l WHERE l.adoption_id = a.id AND l.status = 'open') "
             "AND NOT EXISTS (SELECT 1 FROM cat_outings o WHERE o.adoption_id = a.id AND o.claimed_at IS NULL)")


@dataclass(frozen=True)
class Overview:
    cats: int
    pending: int                                  # coins waiting to be collected
    estimate_per_day: int
    rent_per_day: int
    home_until: Optional[datetime]
    home_on: bool
    outings_out: int                              # cats still away
    outings_ready: int                            # back and waiting to be claimed
    heists_left: int
    loss_left: int                                # coins the games still allow losing today


async def overview(owner_id: int, clone_id) -> Overview:
    pool = await _pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        st = await conn.fetchrow("SELECT * FROM cat_life_state WHERE owner_id = $1 AND clone_key = $2", owner_id, _key(clone_id))
        rows = await conn.fetch("SELECT rarity, cat_rank, hungry_since, adopted_at FROM cat_adoptions "
                                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2", owner_id, _key(clone_id))
        out = await conn.fetch("SELECT ends_at FROM cat_outings WHERE owner_id = $1 AND clone_key = $2 AND claimed_at IS NULL",
                               owner_id, _key(clone_id))
        day = await conn.fetchrow("SELECT gamble_net, heists FROM cat_life_daily WHERE owner_id = $1 AND day = $2",
                                  owner_id, today())
    collected = st["collected_at"] if st else None
    cats = [EarnCat(r["rarity"], int(r["cat_rank"] or 0), earn_since(collected, r["adopted_at"], now),
                    hungry=r["hungry_since"] is not None) for r in rows]
    hs, hu = (st["home_since"], st["home_until"]) if st else (None, None)
    net = int(day["gamble_net"]) if day else 0
    return Overview(
        cats=len(rows), pending=earnings(cats, now, hs, hu), estimate_per_day=daily_earning_estimate(cats),
        rent_per_day=rent_cost(len(rows)), home_until=hu, home_on=bool(hu and _utc(hu) > now),
        outings_out=sum(1 for o in out if _utc(o["ends_at"]) > now), outings_ready=sum(1 for o in out if _utc(o["ends_at"]) <= now),
        heists_left=max(0, HEISTS_PER_DAY - (int(day["heists"]) if day else 0)),
        loss_left=max(0, DAILY_LOSS_LIMIT + min(0, net)))


@dataclass(frozen=True)
class CollectResult:
    status: str                                   # ok | nothing
    coins: int = 0


async def _settle(conn, owner_id: int, clone_id, st, now: datetime) -> int:
    """Pay what the cats earned so far and restart the clock (inside the caller's transaction). Returns the coins paid."""
    rows = await conn.fetch("SELECT rarity, cat_rank, hungry_since, adopted_at FROM cat_adoptions "
                            "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2", owner_id, _key(clone_id))
    cats = [EarnCat(r["rarity"], int(r["cat_rank"] or 0), earn_since(st["collected_at"], r["adopted_at"], now),
                    hungry=r["hungry_since"] is not None) for r in rows]
    coins = earnings(cats, now, st["home_since"], st["home_until"])
    await conn.execute("UPDATE cat_life_state SET collected_at = $3 WHERE owner_id = $1 AND clone_key = $2",
                       owner_id, _key(clone_id), now)
    if coins > 0:
        await _pay(conn, owner_id, coins=coins)
    return max(0, coins)


async def collect(owner_id: int, clone_id) -> CollectResult:
    pool = await _pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            st = await _state(conn, owner_id, clone_id)
            coins = await _settle(conn, owner_id, clone_id, st, now)
    return CollectResult("ok", coins) if coins > 0 else CollectResult("nothing")


@dataclass(frozen=True)
class RentResult:
    status: str                                   # ok | poor | too_far | no_cats
    cost: int = 0
    until: Optional[datetime] = None
    coins: int = 0
    settled: int = 0                              # earnings paid out while renewing a lapsed rent


async def pay_rent(owner_id: int, clone_id, days: int = 1) -> RentResult:
    pool = await _pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            st = await _state(conn, owner_id, clone_id)
            n = int(await conn.fetchval("SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                                        owner_id, _key(clone_id)) or 0)
            if n == 0:
                return RentResult("no_cats")
            until = _utc(st["home_until"])
            base = until if until and until > now else now
            if base + timedelta(days=days) > now + timedelta(days=RENT_MAX_PREPAID_DAYS):
                return RentResult("too_far", until=until)
            cost = rent_cost(n, days)
            left = await _spend(conn, owner_id, cost)
            if left is None:
                return RentResult("poor", cost=cost)
            settled = 0
            if not (until and until > now):
                # The rent had run out: pay what the cats earned so far first, so a late payment can't turn old hours into paid ones.
                settled = await _settle(conn, owner_id, clone_id, st, now)
                left += settled
            since = _utc(st["home_since"]) if until and until > now else now
            new_until = base + timedelta(days=days)
            await conn.execute("UPDATE cat_life_state SET home_since = $3, home_until = $4 WHERE owner_id = $1 AND clone_key = $2",
                               owner_id, _key(clone_id), since or now, new_until)
            return RentResult("ok", cost=cost, until=new_until, coins=left, settled=settled)


async def free_cats(owner_id: int, clone_id) -> list:
    """Cats that can go out: fed, not for sale, not already out."""
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT a.* FROM cat_adoptions a WHERE a.owner_id = $1 AND COALESCE(a.clone_id, -1) = $2 "
            "AND a.hungry_since IS NULL AND " + _FREE_CAT + " ORDER BY a.adopted_at, a.id LIMIT 25", owner_id, _key(clone_id))


@dataclass(frozen=True)
class OutingStart:
    status: str          # ok | poor | busy | hungry | gone | daily_limit | bad_route
    cost: int = 0
    coins: int = 0       # coins left after the fee
    ends_at: Optional[datetime] = None
    cat_name: str = ""
    chance: float = 0.0  # heists: the chance that was used


async def _start(owner_id: int, clone_id, cat_id: int, kind: str, route: str, fee: int, delay: timedelta,
                 roll) -> OutingStart:
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cat = await conn.fetchrow(
                "SELECT a.* FROM cat_adoptions a WHERE a.id = $1 AND a.owner_id = $2 AND COALESCE(a.clone_id, -1) = $3 FOR UPDATE",
                cat_id, owner_id, _key(clone_id))
            if cat is None:
                return OutingStart("gone")
            if cat["hungry_since"] is not None:
                return OutingStart("hungry", cat_name=cat["name"])
            if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open'", cat_id) or \
                    await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", cat_id):
                return OutingStart("busy", cat_name=cat["name"])
            if kind == "heist":
                day = await _daily(conn, owner_id)
                if int(day["heists"]) >= HEISTS_PER_DAY:
                    return OutingStart("daily_limit", cat_name=cat["name"])
            left = await _spend(conn, owner_id, fee)
            if left is None:
                return OutingStart("poor", cost=fee, cat_name=cat["name"])
            success, payout, diamonds, story, chance = roll(cat)
            ends = datetime.now(timezone.utc) + delay
            await conn.execute(
                "INSERT INTO cat_outings (owner_id, clone_key, adoption_id, kind, route, fee, payout, diamonds, success, story, ends_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                owner_id, _key(clone_id), cat_id, kind, route, int(fee), int(payout), int(diamonds), bool(success), story, ends)
            if kind == "heist":
                await conn.execute("UPDATE cat_life_daily SET heists = heists + 1 WHERE owner_id = $1 AND day = $2", owner_id, today())
            return OutingStart("ok", cost=fee, coins=left, ends_at=ends, cat_name=cat["name"], chance=chance)


async def start_walk(owner_id: int, clone_id, cat_id: int, route: str, rng=_RNG) -> OutingStart:
    walk = WALKS.get(route)
    if walk is None:
        return OutingStart("bad_route")

    def roll(cat):
        coins, diamonds = walk_payout(walk, cat["rarity"], int(cat["cat_rank"] or 0), rng)
        return True, coins, diamonds, walk_story(cat["name"], coins, walk.fee, rng), 0.0

    return await _start(owner_id, clone_id, cat_id, "walk", route, walk.fee, timedelta(hours=walk.hours), roll)


async def start_heist(owner_id: int, clone_id, cat_id: int, route: str, rng=_RNG) -> OutingStart:
    heist = HEISTS.get(route)
    if heist is None:
        return OutingStart("bad_route")

    def roll(cat):
        rank = int(cat["cat_rank"] or 0)
        success, payout = heist_roll(heist, cat["rarity"], rank, rng)
        return success, payout, 0, heist_story(cat["name"], success, payout, rng), heist_chance(heist, cat["rarity"], rank)

    return await _start(owner_id, clone_id, cat_id, "heist", route, heist.fee, timedelta(seconds=heist.seconds), roll)


@dataclass(frozen=True)
class Outing:
    id: int
    cat_name: str
    kind: str
    route: str
    fee: int
    payout: int
    diamonds: int
    success: bool
    story: str
    ends_at: datetime


async def claim_outings(owner_id: int, clone_id) -> List[Outing]:
    """Pay every finished outing and mark it claimed, in one transaction. Returns what was paid."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT o.*, a.name AS cat_name FROM cat_outings o JOIN cat_adoptions a ON a.id = o.adoption_id "
                "WHERE o.owner_id = $1 AND o.clone_key = $2 AND o.claimed_at IS NULL AND o.ends_at <= NOW() "
                "ORDER BY o.ends_at FOR UPDATE OF o", owner_id, _key(clone_id))
            if not rows:
                return []
            await conn.execute("UPDATE cat_outings SET claimed_at = NOW() WHERE id = ANY($1::bigint[])", [int(r["id"]) for r in rows])
            await _pay(conn, owner_id, coins=sum(int(r["payout"]) for r in rows), diamonds=sum(int(r["diamonds"]) for r in rows))
    return [Outing(int(r["id"]), r["cat_name"], r["kind"], r["route"], int(r["fee"]), int(r["payout"]), int(r["diamonds"]),
                   bool(r["success"]), r["story"], r["ends_at"]) for r in rows]


async def open_outings(owner_id: int, clone_id) -> list:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT o.kind, o.route, o.ends_at, a.name AS cat_name FROM cat_outings o JOIN cat_adoptions a ON a.id = o.adoption_id "
            "WHERE o.owner_id = $1 AND o.clone_key = $2 AND o.claimed_at IS NULL ORDER BY o.ends_at", owner_id, _key(clone_id))


@dataclass(frozen=True)
class GambleResult:
    status: str            # ok | poor | limit | bad_bet
    game: str = ""
    bet: int = 0
    payout: int = 0
    multiplier: float = 0.0
    coins: int = 0
    loss_left: int = 0


async def gamble(owner_id: int, game: str, bet: int, rng=_RNG) -> GambleResult:
    if game not in ("flip", "spin") or bet not in BETS:
        return GambleResult("bad_bet", game=game, bet=bet)
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            day = await _daily(conn, owner_id)
            net = int(day["gamble_net"])
            if bet > DAILY_LOSS_LIMIT + min(0, net):          # this bet could take the day's losses past the limit
                return GambleResult("limit", game=game, bet=bet, loss_left=max(0, DAILY_LOSS_LIMIT + min(0, net)))
            left = await _spend(conn, owner_id, bet)
            if left is None:
                return GambleResult("poor", game=game, bet=bet)
            if game == "flip":
                mult = float(FLIP_MULTIPLIER) if flip_roll(rng) else 0.0
            else:
                mult = spin_roll(rng)
            payout = int(bet * mult)
            if payout:
                await _pay(conn, owner_id, coins=payout)
                left += payout
            new_net = net + payout - bet
            await conn.execute("UPDATE cat_life_daily SET gamble_net = $3 WHERE owner_id = $1 AND day = $2", owner_id, today(), new_net)
            return GambleResult("ok", game, bet, payout, mult, left, max(0, DAILY_LOSS_LIMIT + min(0, new_net)))
