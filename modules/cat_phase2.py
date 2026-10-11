"""Phase-two cat game rules: quests, auctions, and collection badges."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from modules import cat_adopt

QUESTS = {
    "adopt-2": ("Adopt 2 cats", 2, 2),
    "rank-1": ("Rank one cat up", 1, 3),
    "rare-1": ("Adopt a rare cat", 1, 4),
}


@dataclass(frozen=True)
class Quest:
    key: str
    title: str
    progress: int
    target: int
    reward: int
    claimed: bool


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id):
    return -1 if clone_id is None else int(clone_id)


async def quests(owner_id: int, clone_id, today: Optional[date] = None) -> list[Quest]:
    day = today or datetime.now(timezone.utc).date()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM cat_daily_quests WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 AND quest_date=$3 ORDER BY quest_key", owner_id, _key(clone_id), day)
    existing = {r["quest_key"]: r for r in rows}
    return [Quest(key, title, int(existing[key]["progress"]) if key in existing else 0, target, reward, bool(existing[key]["claimed_at"]) if key in existing else False) for key, (title, target, reward) in QUESTS.items()]


async def record_progress(owner_id: int, clone_id, event: str, amount: int = 1, rarity: Optional[str] = None) -> None:
    day = datetime.now(timezone.utc).date()
    keys = ["adopt-2"] if event == "adopt" else ["rank-1"] if event == "rank" else ["rare-1"] if event == "adopt" and rarity in ("rare", "legendary") else []
    if event == "adopt" and rarity in ("rare", "legendary"):
        keys.append("rare-1")
    if not keys:
        return
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key in set(keys):
                _, target, reward = QUESTS[key]
                await conn.execute("INSERT INTO cat_daily_quests(owner_id,clone_id,quest_date,quest_key,progress,target,diamonds_reward) VALUES($1,$2,$3,$4,LEAST($5,$6),$5,$7) ON CONFLICT(owner_id,clone_key,quest_date,quest_key) DO UPDATE SET progress=LEAST(cat_daily_quests.target,cat_daily_quests.progress+$5),updated_at=NOW()", owner_id, clone_id, day, key, amount, target, reward)


async def claim_quest(owner_id: int, clone_id, key: str) -> str:
    if key not in QUESTS:
        return "missing"
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            day = datetime.now(timezone.utc).date()
            row = await conn.fetchrow("UPDATE cat_daily_quests SET claimed_at=NOW() WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 AND quest_date=$3 AND quest_key=$4 AND progress>=target AND claimed_at IS NULL RETURNING diamonds_reward", owner_id, _key(clone_id), day, key)
            if not row:
                return "not_ready"
            await conn.execute("INSERT INTO cat_wallets(owner_id,clone_id,diamonds) VALUES($1,$2,$3) ON CONFLICT(owner_id,(COALESCE(clone_id,-1))) DO UPDATE SET diamonds=cat_wallets.diamonds+EXCLUDED.diamonds,updated_at=NOW()", owner_id, clone_id, int(row["diamonds_reward"]))
    return "ok"


async def award_badges(owner_id: int, clone_id) -> list[str]:
    pool = await _pool()
    async with pool.acquire() as conn:
        cats = await conn.fetch("SELECT rarity,trait FROM cat_adoptions WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2", owner_id, _key(clone_id))
        traits = {r["trait"] for r in cats}
        eligible = ({"coat-collector"} if len(traits) >= 5 else set()) | ({"rare-hunter"} if any(r["rarity"] in ("rare", "legendary") for r in cats) else set()) | ({"full-house"} if len(cats) >= 10 else set())
        awarded = []
        for slug in eligible:
            row = await conn.fetchrow("INSERT INTO cat_badge_awards(owner_id,clone_id,badge_slug) SELECT $1,$2,slug FROM cat_badges WHERE slug=$3 ON CONFLICT DO NOTHING RETURNING badge_slug", owner_id, clone_id, slug)
            if row:
                reward = await conn.fetchval("SELECT reward_diamonds FROM cat_badges WHERE slug=$1", slug)
                await conn.execute("INSERT INTO cat_wallets(owner_id,clone_id,diamonds) VALUES($1,$2,$3) ON CONFLICT(owner_id,(COALESCE(clone_id,-1))) DO UPDATE SET diamonds=cat_wallets.diamonds+EXCLUDED.diamonds,updated_at=NOW()", owner_id, clone_id, int(reward))
                awarded.append(slug)
    return awarded


async def create_auction(owner_id: int, clone_id, cat_id: int, starting_price: int, guild_id=None, channel_id=None) -> str:
    if starting_price <= 0:
        return "invalid"
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO cat_auctions(adoption_id,seller_id,seller_clone_id,guild_id,channel_id,starting_price,ends_at) SELECT id,$2,$3,$4,$5,$6,NOW()+INTERVAL '24 hours' FROM cat_adoptions WHERE id=$1 AND owner_id=$2 AND COALESCE(clone_id,-1)=$7 AND NOT EXISTS (SELECT 1 FROM cat_auctions WHERE adoption_id=$1 AND status='open') RETURNING id", cat_id, owner_id, clone_id, guild_id, channel_id, starting_price, _key(clone_id))
    return "ok" if row else "unavailable"


async def bid(bidder_id: int, clone_id, auction_id: int, amount: int) -> str:
    if amount <= 0:
        return "invalid"
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            auction = await conn.fetchrow("SELECT * FROM cat_auctions WHERE id=$1 AND status='open' AND ends_at>NOW() FOR UPDATE", auction_id)
            if not auction or auction["seller_id"] == bidder_id:
                return "unavailable"
            minimum = max(int(auction["starting_price"]), int(auction["highest_bid"] or 0) + 1)
            if amount < minimum:
                return "low"
            wallet = await conn.fetchrow("SELECT coins FROM cat_wallets WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 FOR UPDATE", bidder_id, _key(clone_id))
            if not wallet or int(wallet["coins"]) < amount:
                return "poor"
            await conn.execute("UPDATE cat_wallets SET coins=coins-$3,updated_at=NOW() WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2", bidder_id, _key(clone_id), amount)
            if auction["highest_bidder_id"]:
                await conn.execute("UPDATE cat_wallets SET coins=coins+$3,updated_at=NOW() WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2", auction["highest_bidder_id"], _key(auction["highest_bidder_clone_id"]), int(auction["highest_bid"]))
            await conn.execute("UPDATE cat_auctions SET highest_bid=$2,highest_bidder_id=$3,highest_bidder_clone_id=$4 WHERE id=$1", auction_id, amount, bidder_id, clone_id)
    return "ok"
