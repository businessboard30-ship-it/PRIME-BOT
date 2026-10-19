# path: modules/cat_stats.py

"""
Cat stats and skills (play game only, coins only, never real money). Phase 1 of the fights plan.

  * Four stats: Strength (damage), Guard (damage reduction and HP), Speed (who acts first, dodge), Spirit (crit, skill power).
  * Points pool = rarity base + 2 per rank. Points stay unspent until the owner allocates them. No single stat above 60% of the pool.
  * Unspent points are DERIVED (pool - allocated), so ranking up gives more points with no extra write.
  * Trait bonus: a free +1 on one stat (data table below). It is not part of the pool and not capped.
  * Applying points only ever ADDS. Taking points back is a respec: free once, then 10,000 + 2,000 x rank coins.
  * Skills: 1 slot at rank 0, 2 at rank 5, 3 at rank 12. Each skill has a rank requirement and a cooldown (data, not code).
  * Stats follow the cat by id, so they stay with it when it is traded.

Pure rules at the top (no database, no Discord). Every change below is ONE transaction with the cat row locked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from modules import cat_rank

logger = logging.getLogger(__name__)

STATS = ("strength", "guard", "speed", "spirit")
STAT_LABEL = {"strength": "Strength", "guard": "Guard", "speed": "Speed", "spirit": "Spirit"}
STAT_EMOJI = {"strength": "🦾", "guard": "🪖", "speed": "☄️", "spirit": "🪬"}
STAT_HELP = {"strength": "damage", "guard": "less damage taken, more HP", "speed": "acts first, dodges",
             "spirit": "crits and skill power"}

BASE_POINTS = {"common": 8, "uncommon": 10, "rare": 13, "legendary": 16}
POINTS_PER_RANK = 2
CAP_SHARE = 0.60                    # no single stat above 60% of the pool
TRAIT_BONUS = {"Brave": "strength", "Chonky": "guard", "Gentle": "guard", "Zoomies": "speed",
               "Dreamy": "spirit", "Sassy": "spirit"}               # everything else: none
RESPEC_BASE, RESPEC_PER_RANK = 10_000, 2_000

# (rank needed, slots): 1 slot at rank 0, 2 at rank 5, 3 at rank 12
SLOT_RANKS = (0, 5, 12)
MAX_SLOTS = len(SLOT_RANKS)


@dataclass(frozen=True)
class Skill:
    key: str
    name: str
    emoji: str
    text: str
    min_rank: int
    cooldown: int           # rounds


SKILLS: Dict[str, Skill] = {s.key: s for s in (
    Skill("pounce", "Pounce", "🐾", "Big hit, but slow to recover.", 0, 3),
    Skill("hiss", "Hiss", "🫨", "Lowers the other cat's Strength for 1 round.", 0, 3),
    Skill("fluff_guard", "Fluff Guard", "🫧", "Fluffs up and blocks a hit.", 0, 4),
    Skill("nap_heal", "Nap Heal", "🪷", "A small heal, once a fight.", 3, 8),
    Skill("zoomies_dodge", "Zoomies Dodge", "🪽", "The next hit misses.", 5, 4),
    Skill("lucky_paw", "Lucky Paw", "🧿", "Extra crit chance.", 5, 3),
    Skill("counter_claw", "Counter Claw", "🪝", "Hands back part of the damage.", 8, 4),
    Skill("yarn_trap", "Yarn Trap", "🕸️", "The other cat skips a turn. Long cooldown.", 12, 6),
)}


# ── pure rules ──────────────────────────────────────────────────────────

def pool(rarity: str, rank) -> int:
    """Points the owner can spend in total: rarity base + 2 per rank."""
    return BASE_POINTS.get(str(rarity), BASE_POINTS["common"]) + POINTS_PER_RANK * cat_rank.clamp(rank)


def stat_cap(total_pool: int) -> int:
    """The most points one stat may hold (60% of the pool, rounded down)."""
    return int(total_pool * CAP_SHARE)


def trait_bonus(trait: str) -> Dict[str, int]:
    stat = TRAIT_BONUS.get(str(trait))
    return {s: (1 if s == stat else 0) for s in STATS}


def respec_cost(rank, respec_count: int) -> int:
    """Free the first time, then 10,000 + 2,000 x rank coins."""
    if int(respec_count or 0) < 1:
        return 0
    return RESPEC_BASE + RESPEC_PER_RANK * cat_rank.clamp(rank)


def slots_for(rank) -> int:
    r = cat_rank.clamp(rank)
    return sum(1 for need in SLOT_RANKS if r >= need)


def skills_for(rank) -> List[Skill]:
    """Skills a cat of this rank may equip, in list order."""
    r = cat_rank.clamp(rank)
    return [s for s in SKILLS.values() if r >= s.min_rank]


@dataclass(frozen=True)
class Stats:
    strength: int = 0
    guard: int = 0
    speed: int = 0
    spirit: int = 0
    respec_count: int = 0
    skills: Tuple[str, ...] = ()

    def alloc(self) -> Dict[str, int]:
        return {s: int(getattr(self, s)) for s in STATS}

    def spent(self) -> int:
        return sum(self.alloc().values())


def totals(stats: Stats, trait: str) -> Dict[str, int]:
    """Allocated points plus the free trait bonus: what the battle engine will use."""
    bonus = trait_bonus(trait)
    return {s: int(getattr(stats, s)) + bonus[s] for s in STATS}


def unspent(stats: Stats, rarity: str, rank) -> int:
    return max(0, pool(rarity, rank) - stats.spent())


def validate_alloc(new: Dict[str, int], old: Dict[str, int], rarity: str, rank) -> Optional[str]:
    """None when `new` is a legal allocation that only adds to `old`, else a short reason code."""
    total = pool(rarity, rank)
    cap = stat_cap(total)
    for s in STATS:
        v = new.get(s)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return "invalid"
        if v < int(old.get(s, 0)):
            return "shrink"                 # taking points back is a respec
        if v > cap:
            return "cap"
    if sum(int(new[s]) for s in STATS) > total:
        return "pool"
    return None


def validate_skills(keys: Sequence[str], rank) -> Optional[str]:
    if len(keys) > slots_for(rank):
        return "slots"
    if len(set(keys)) != len(keys):
        return "dupe"
    r = cat_rank.clamp(rank)
    for k in keys:
        sk = SKILLS.get(k)
        if sk is None:
            return "unknown"
        if r < sk.min_rank:
            return "rank"
    return None


def power_rating(stats: Stats, trait: str) -> int:
    """Stats part of the Power Rating (skills add 2 each, items later)."""
    return sum(totals(stats, trait).values()) + 2 * len(stats.skills)


# ── screen text (pure) ──────────────────────────────────────────────────

def stat_lines(stats: Stats, trait: str, pending: Optional[Dict[str, int]] = None) -> str:
    """One line per stat. `pending` are points picked on screen but not applied yet."""
    bonus = trait_bonus(trait)
    pending = pending or {}
    out = []
    for s in STATS:
        have = int(getattr(stats, s))
        add = int(pending.get(s, 0))
        extra = f" **+{add}**" if add else ""
        free = f" (+{bonus[s]} trait)" if bonus[s] else ""
        out.append(f"{STAT_EMOJI[s]} **{STAT_LABEL[s]}** {have}{extra}{free} · {STAT_HELP[s]}")
    return "\n".join(out)


def skill_lines(keys: Sequence[str], rank) -> str:
    slots = slots_for(rank)
    out = []
    for i in range(MAX_SLOTS):
        if i < len(keys) and keys[i] in SKILLS:
            sk = SKILLS[keys[i]]
            out.append(f"{i + 1}. {sk.emoji} **{sk.name}** · {sk.text} (every {sk.cooldown} rounds)")
        elif i < slots:
            out.append(f"{i + 1}. *empty slot*")
        else:
            out.append(f"{i + 1}. 🔒 unlocks at rank {SLOT_RANKS[i]}")
    return "\n".join(out)


# ── database ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StatsResult:
    status: str     # ok | not_yours | stale | listed | invalid | shrink | cap | pool | slots | dupe | unknown | rank | poor
    stats: Stats = field(default_factory=Stats)
    cost: int = 0
    coins: int = 0          # coins left (ok) or coins the person has (poor)


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def _load(conn, cat_id: int, lock: bool = False) -> Stats:
    row = await conn.fetchrow("SELECT strength, guard, speed, spirit, respec_count FROM cat_stats WHERE cat_id = $1"
                              + (" FOR UPDATE" if lock else ""), cat_id)
    rows = await conn.fetch("SELECT skill_key FROM cat_skills WHERE cat_id = $1 ORDER BY slot", cat_id)
    keys = tuple(r["skill_key"] for r in rows if r["skill_key"] in SKILLS)
    if row is None:
        return Stats(skills=keys)
    return Stats(int(row["strength"]), int(row["guard"]), int(row["speed"]), int(row["spirit"]),
                 int(row["respec_count"]), keys)


async def get_stats(cat_id: int) -> Stats:
    """A cat's stats and skills. Never raises: a database problem shows an empty set instead of breaking My cats."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            return await _load(conn, cat_id)
    except Exception:
        logger.debug("[cat-stats] couldn't read stats for cat %s", cat_id, exc_info=True)
        return Stats()


async def get_many(cat_ids: Sequence[int]) -> Dict[int, Stats]:
    """Stats for several cats in two queries (for lists). Cats with none get an empty Stats."""
    ids = [int(i) for i in cat_ids]
    out: Dict[int, Stats] = {i: Stats() for i in ids}
    if not ids:
        return out
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch(
                "SELECT cat_id, strength, guard, speed, spirit, respec_count FROM cat_stats WHERE cat_id = ANY($1::bigint[])", ids)
            srows = await conn.fetch(
                "SELECT cat_id, skill_key FROM cat_skills WHERE cat_id = ANY($1::bigint[]) ORDER BY cat_id, slot", ids)
        keys: Dict[int, List[str]] = {}
        for r in srows:
            if r["skill_key"] in SKILLS:
                keys.setdefault(int(r["cat_id"]), []).append(r["skill_key"])
        for r in rows:
            out[int(r["cat_id"])] = Stats(int(r["strength"]), int(r["guard"]), int(r["speed"]), int(r["spirit"]),
                                          int(r["respec_count"]), tuple(keys.get(int(r["cat_id"]), ())))
        for cid, ks in keys.items():
            if cid in out and not out[cid].skills:
                out[cid] = Stats(*[getattr(out[cid], s) for s in STATS], out[cid].respec_count, tuple(ks))
    except Exception:
        logger.debug("[cat-stats] couldn't read stats for a list of cats", exc_info=True)
    return out


async def _lock_cat(conn, cat_id: int, owner_id: int, clone_id, expect_rank: Optional[int]):
    """Lock the cat row and check it: yours, rank unchanged, not for sale. Returns (row, None) or (None, status)."""
    row = await conn.fetchrow("SELECT owner_id, clone_id, rarity, cat_rank FROM cat_adoptions WHERE id = $1 FOR UPDATE", cat_id)
    if row is None or int(row["owner_id"]) != owner_id or _key(row["clone_id"]) != _key(clone_id):
        return None, "not_yours"
    if expect_rank is not None and cat_rank.clamp(row["cat_rank"]) != cat_rank.clamp(expect_rank):
        return None, "stale"
    if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open' UNION ALL SELECT 1 FROM market_listings WHERE item_type = 'cat' AND item_ref = $1 AND status = 'open'", cat_id):
        return None, "listed"
    return row, None


async def apply_points(cat_id: int, owner_id: int, clone_id, expect_rank: int, new: Dict[str, int]) -> StatsResult:
    """Set the allocated points to `new` (absolute values; every stat must be >= what it is now)."""
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row, bad = await _lock_cat(conn, cat_id, owner_id, clone_id, expect_rank)
            if bad:
                return StatsResult(bad)
            await conn.execute("INSERT INTO cat_stats (cat_id) VALUES ($1) ON CONFLICT (cat_id) DO NOTHING", cat_id)
            cur = await _load(conn, cat_id, lock=True)
            why = validate_alloc(new, cur.alloc(), row["rarity"], row["cat_rank"])
            if why:
                return StatsResult(why, cur)
            await conn.execute(
                "UPDATE cat_stats SET strength = $2, guard = $3, speed = $4, spirit = $5, updated_at = NOW() WHERE cat_id = $1",
                cat_id, *(int(new[s]) for s in STATS))
            return StatsResult("ok", await _load(conn, cat_id))


async def respec(cat_id: int, owner_id: int, clone_id, expect_rank: int, expect_count: int) -> StatsResult:
    """Give all points back. Free the first time, then coins. Skills stay equipped (they follow rank, not points).
    `expect_count` is the respec count the person saw, so a double tap can never charge twice."""
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row, bad = await _lock_cat(conn, cat_id, owner_id, clone_id, expect_rank)
            if bad:
                return StatsResult(bad)
            await conn.execute("INSERT INTO cat_stats (cat_id) VALUES ($1) ON CONFLICT (cat_id) DO NOTHING", cat_id)
            cur = await _load(conn, cat_id, lock=True)
            if cur.respec_count != int(expect_count):
                return StatsResult("stale", cur)
            cost = respec_cost(row["cat_rank"], cur.respec_count)
            left = 0
            if cost:
                left = await conn.fetchval(
                    "UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                    "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                    owner_id, -1, cost)             # -1: the wallet is shared by every bot
                if left is None:
                    have = await conn.fetchval(
                        "SELECT coins FROM cat_wallets WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2", owner_id, -1)
                    return StatsResult("poor", cur, cost, int(have or 0))
            await conn.execute(
                "UPDATE cat_stats SET strength = 0, guard = 0, speed = 0, spirit = 0, respec_count = respec_count + 1, "
                "updated_at = NOW() WHERE cat_id = $1", cat_id)
            return StatsResult("ok", await _load(conn, cat_id), cost, int(left or 0))


async def set_skills(cat_id: int, owner_id: int, clone_id, expect_rank: int, keys: Sequence[str]) -> StatsResult:
    """Equip these skills (in order, one per slot). Free to change."""
    keys = list(keys)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row, bad = await _lock_cat(conn, cat_id, owner_id, clone_id, expect_rank)
            if bad:
                return StatsResult(bad)
            why = validate_skills(keys, row["cat_rank"])
            if why:
                return StatsResult(why)
            await conn.execute("DELETE FROM cat_skills WHERE cat_id = $1", cat_id)
            for slot, k in enumerate(keys, start=1):
                await conn.execute("INSERT INTO cat_skills (cat_id, slot, skill_key) VALUES ($1, $2, $3)", cat_id, slot, k)
            return StatsResult("ok", await _load(conn, cat_id))
