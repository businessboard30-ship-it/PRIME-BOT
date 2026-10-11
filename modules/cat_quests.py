"""Cat game: daily quests and collection badges (play-only diamond rewards).

Quests reset every UTC day. Progress is recorded by `on_adopt` / `on_rank`, which the adopt and rank-up screens call
after the real action succeeded; they never raise, so a quest problem can never undo or block an adoption.
Badges are one-time: the award row and the diamond reward are written in one transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from modules import cat_adopt

logger = logging.getLogger(__name__)

# key -> (title, target, diamond reward). Tune the economy here.
QUESTS = {
    "adopt-2": ("Adopt 2 cats", 2, 1),
    "rank-1": ("Rank one cat up", 1, 1),
    "rare-1": ("Adopt a rare or legendary cat", 1, 1),
}

# slug -> (emoji, name, how to earn it, diamond reward)
BADGES = {
    "coat-collector": ("🎨", "Coat Collector", "Own cats with 5 different traits", 2),
    "full-set": ("🌈", "Full Set", f"Own every one of the {len(cat_adopt.TRAITS)} traits", 10),
    "rare-hunter": ("🌟", "Rare Hunter", "Own a rare or legendary cat", 1),
    "legend": ("👑", "Legend Keeper", "Own a legendary cat", 2),
    "full-house": ("🏡", "Full House", "Own 10 cats at once", 2),
}


@dataclass(frozen=True)
class Quest:
    key: str
    title: str
    progress: int
    target: int
    reward: int
    claimed: bool

    @property
    def ready(self) -> bool:
        return self.progress >= self.target and not self.claimed


def today() -> date:
    return datetime.now(timezone.utc).date()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


def quest_keys_for(event: str, rarity: Optional[str] = None) -> list:
    """Which quests move forward for an event (pure)."""
    if event == "adopt":
        keys = ["adopt-2"]
        if rarity in ("rare", "legendary"):
            keys.append("rare-1")
        return keys
    if event == "rank":
        return ["rank-1"]
    return []


def earned_badges(cats: Iterable[tuple]) -> set:
    """Badges a collection qualifies for. `cats` is (rarity, trait) pairs (pure)."""
    cats = list(cats)
    traits = {t for _, t in cats}
    out = set()
    if len(traits) >= 5:
        out.add("coat-collector")
    if set(cat_adopt.TRAITS) <= traits:
        out.add("full-set")
    if any(r in ("rare", "legendary") for r, _ in cats):
        out.add("rare-hunter")
    if any(r == "legendary" for r, _ in cats):
        out.add("legend")
    if len(cats) >= 10:
        out.add("full-house")
    return out


async def quests(owner_id: int, clone_id, day: Optional[date] = None) -> list:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT quest_key, progress, claimed_at FROM cat_daily_quests "
            "WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 AND quest_date=$3", owner_id, _key(clone_id), day or today())
    have = {r["quest_key"]: r for r in rows}
    return [Quest(k, title, int(have[k]["progress"]) if k in have else 0, target, reward,
                  bool(k in have and have[k]["claimed_at"]))
            for k, (title, target, reward) in QUESTS.items()]


async def record_progress(owner_id: int, clone_id, keys: Iterable[str], amount: int = 1,
                          day: Optional[date] = None) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key in dict.fromkeys(keys):
                if key not in QUESTS:
                    continue
                _, target, reward = QUESTS[key]
                await conn.execute(
                    "INSERT INTO cat_daily_quests (owner_id, clone_id, quest_date, quest_key, progress, target, diamonds_reward) "
                    "VALUES ($1,$2,$3,$4,LEAST($5::int,$6::int),$6,$7) "
                    "ON CONFLICT (owner_id, clone_key, quest_date, quest_key) DO UPDATE SET "
                    "progress = LEAST(cat_daily_quests.target, cat_daily_quests.progress + $5::int), updated_at = NOW()",
                    owner_id, clone_id, day or today(), key, amount, target, reward)


async def claim(owner_id: int, clone_id, key: str, day: Optional[date] = None) -> str:
    """ok | not_ready | missing. The diamonds arrive in the same transaction that marks the quest claimed."""
    if key not in QUESTS:
        return "missing"
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE cat_daily_quests SET claimed_at = NOW() WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 "
                "AND quest_date=$3 AND quest_key=$4 AND progress >= target AND claimed_at IS NULL "
                "RETURNING diamonds_reward", owner_id, _key(clone_id), day or today(), key)
            if row is None:
                return "not_ready"
            await _pay_diamonds(conn, owner_id, clone_id, int(row["diamonds_reward"]))
    return "ok"


async def _pay_diamonds(conn, owner_id, clone_id, diamonds: int) -> None:
    await conn.execute(
        "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1,$2,0,$3) "
        "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
        "diamonds = cat_wallets.diamonds + EXCLUDED.diamonds, updated_at = NOW()", owner_id, clone_id, diamonds)


async def owned_badges(owner_id: int, clone_id) -> set:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT badge_slug FROM cat_badge_awards WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2",
                                owner_id, _key(clone_id))
    return {r["badge_slug"] for r in rows}


async def award_badges(owner_id: int, clone_id) -> list:
    """Award every badge the collection now qualifies for (once each). Returns the newly awarded slugs."""
    pool = await _pool()
    new = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT rarity, trait FROM cat_adoptions WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2",
                owner_id, _key(clone_id))
            for slug in sorted(earned_badges((r["rarity"], r["trait"]) for r in rows)):
                got = await conn.fetchval(
                    "INSERT INTO cat_badge_awards (owner_id, clone_id, badge_slug) VALUES ($1,$2,$3) "
                    "ON CONFLICT DO NOTHING RETURNING badge_slug", owner_id, clone_id, slug)
                if got:
                    await _pay_diamonds(conn, owner_id, clone_id, BADGES[slug][3])
                    new.append(slug)
    return new


def badge_line(slug: str) -> str:
    emoji, name, _, reward = BADGES[slug]
    return f"{emoji} **{name}** (+{reward} 💎)"


async def on_adopt(owner_id: int, clone_id, rarity: str) -> list:
    """After a successful adoption. Never raises. Returns lines to show for any badge just earned."""
    try:
        await record_progress(owner_id, clone_id, quest_keys_for("adopt", rarity))
        return [badge_line(s) for s in await award_badges(owner_id, clone_id)]
    except Exception:
        logger.warning("[cat-quests] adopt hook failed", exc_info=True)
        return []


async def on_rank(owner_id: int, clone_id) -> None:
    try:
        await record_progress(owner_id, clone_id, quest_keys_for("rank"))
    except Exception:
        logger.warning("[cat-quests] rank hook failed", exc_info=True)
