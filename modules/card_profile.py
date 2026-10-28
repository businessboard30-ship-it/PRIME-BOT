# path: modules/card_profile.py

"""
Collector profiles, collection browser, achievements and leaderboards. Data layer only (the Components V2 screens call
these). Every number is derived from real rows:
  * "Owned" for statistics, achievements and rankings means PERMANENT copies (rental_expires_at IS NULL), so rentals
    and expired rentals never count. One card_copies row = one copy with one owner, so nothing double counts.
  * Strength uses card_boost.strength; the SQL below uses the identical formula (half-up rounding).
  * Local leaderboards take the guild's member ids from the caller (server-side); global ones take none.
  * Privacy: a viewer other than the owner only sees what the owner's toggles allow.
"""

from __future__ import annotations

from typing import Optional, Sequence

from modules import card_boost as cb
from modules import card_economy as _ce
from modules.card_economy import StoreError, _clone_key

RARITY_RANK = {"common": 1, "rare": 2, "epic": 3, "legendary": 4, "god": 5, "supreme": 6}
HIGH_RARITY = ("legendary", "god", "supreme")
MAX_FEATURED = 5

# key -> (title, stat, threshold). Rewards are cosmetic (badge/title) only, so there is nothing to farm.
ACHIEVEMENTS = {
    "collector_10": ("Collector", "unique", 10),
    "collector_50": ("Curator", "unique", 50),
    "first_limited": ("Limited Edition", "limited", 1),
    "high_roller": ("High Rarity Holder", "high_rarity", 1),
    "booster_25": ("Powered Up", "boosts", 25),
    "trader_5": ("Trader", "trades", 5),
    "trader_25": ("Dealmaker", "trades", 25),
    "complete_set": ("Completionist", "completion_pct", 100),
}

# SQL expression: strength of one copy (alias cp = card_copies, cc = card_catalog). Must match card_boost.strength.
_STRENGTH_SQL = (f"FLOOR(cc.base_strength * (1 + {cb.MAX_BONUS} * (1 - POWER({cb.DECAY}::float8, cp.boost_level))) + 0.5)")
_RANK_SQL = ("CASE cc.rarity " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in RARITY_RANK.items()) + " ELSE 0 END")

_METRICS = {   # metric -> aggregate over a user's permanent copies
    "unique": "COUNT(DISTINCT cp.card_id)",
    "copies": "COUNT(*)",
    "strength": f"SUM({_STRENGTH_SQL})",
    "boosts": "SUM(cp.boost_level)",
    "limited": "COUNT(*) FILTER (WHERE cp.serial_no IS NOT NULL)",
    "rarity": f"SUM(POWER(4, {_RANK_SQL}))",    # rarer holdings dominate: one supreme outranks many commons
}


async def _perm(conn, user_id, clone_id):
    return ("FROM card_copies cp JOIN card_catalog cc USING (card_id) WHERE cp.owner_id = $1 "
            "AND COALESCE(cp.clone_id,-1) = $2 AND cp.rental_expires_at IS NULL")


async def get_stats(user_id: int, clone_id: Optional[int]) -> dict:
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        frm = await _perm(conn, user_id, clone_id)
        r = await conn.fetchrow(
            f"SELECT COUNT(*) AS copies, COUNT(DISTINCT cp.card_id) AS uniq, "
            f"COUNT(*) FILTER (WHERE cp.serial_no IS NOT NULL) AS limited, "
            f"COUNT(*) FILTER (WHERE cc.rarity = ANY($3::text[])) AS high, COALESCE(SUM(cp.boost_level),0) AS boosts, "
            f"COALESCE(SUM({_STRENGTH_SQL}),0)::bigint AS strength, COALESCE(MAX({_RANK_SQL}),0) AS top_rank {frm}",
            user_id, _clone_key(clone_id), list(HIGH_RARITY))
        trades = await conn.fetchval(
            "SELECT COUNT(*) FROM card_trades WHERE status='completed' AND (initiator_id=$1 OR recipient_id=$1) "
            "AND COALESCE(clone_id,-1)=$2", user_id, _clone_key(clone_id))
        cat = await conn.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE card_id IN (SELECT cp.card_id FROM card_copies cp "
            "WHERE cp.owner_id=$1 AND COALESCE(cp.clone_id,-1)=$2 AND cp.rental_expires_at IS NULL)) AS owned "
            "FROM card_catalog WHERE active AND store_enabled AND COALESCE(clone_id,-1)=$2", user_id, _clone_key(clone_id))
    top = next((k for k, v in RARITY_RANK.items() if v == r["top_rank"]), None)
    pct = int(cat["owned"] * 100 / cat["total"]) if cat["total"] else 0
    return {"copies": r["copies"], "unique": r["uniq"], "limited": r["limited"], "high_rarity": r["high"],
            "boosts": r["boosts"], "strength": r["strength"], "highest_rarity": top, "trades": trades,
            "completion_pct": pct, "catalogue_size": cat["total"]}


async def get_settings(user_id: int, clone_id: Optional[int]) -> dict:
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM card_profiles WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2",
                                  user_id, _clone_key(clone_id))
    return dict(row) if row else {"user_id": user_id, "show_coins": False, "show_collection": True,
                                  "show_trades": True, "featured_copy_ids": [], "signature_copy_id": None,
                                  "title_key": None}


async def update_settings(user_id: int, clone_id: Optional[int], **fields) -> None:
    allowed = {"show_coins", "show_collection", "show_trades"}
    bad = set(fields) - allowed
    if bad:
        raise StoreError("bad_field", "Unknown setting.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO card_profiles (user_id, clone_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                           user_id, clone_id)
        for k, v in fields.items():
            await conn.execute(f"UPDATE card_profiles SET {k}=$3 WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2",
                               user_id, _clone_key(clone_id), bool(v))


async def set_featured(user_id: int, clone_id: Optional[int], copy_ids: Sequence[int],
                       signature: Optional[int] = None) -> None:
    ids = list(dict.fromkeys(copy_ids))
    if len(ids) > MAX_FEATURED:
        raise StoreError("too_many", f"You can feature up to {MAX_FEATURED} cards.")
    if signature is not None and signature not in ids:
        ids.append(signature)
        if len(ids) > MAX_FEATURED:
            raise StoreError("too_many", f"You can feature up to {MAX_FEATURED} cards.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ids:
                n = await conn.fetchval("SELECT COUNT(*) FROM card_copies WHERE copy_id = ANY($1::bigint[]) "
                                        "AND owner_id=$2", ids, user_id)
                if n != len(ids):
                    raise StoreError("not_owner", "You can only feature cards you own.")
            await conn.execute("INSERT INTO card_profiles (user_id, clone_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                               user_id, clone_id)
            await conn.execute("UPDATE card_profiles SET featured_copy_ids=$3, signature_copy_id=$4 "
                               "WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2", user_id, _clone_key(clone_id), ids, signature)


async def _card_view(conn, copy_ids):
    """Featured cards that are STILL owned by the user (traded-away/expired ones silently drop out)."""
    rows = await conn.fetch(
        f"SELECT cp.copy_id, cp.serial_no, cp.boost_level, cp.rental_expires_at, cc.name, cc.rarity, cc.image_url, "
        f"cc.stock_total, cc.skills, cc.base_strength, {_STRENGTH_SQL}::int AS strength "
        f"FROM card_copies cp JOIN card_catalog cc USING (card_id) WHERE cp.copy_id = ANY($1::bigint[])", copy_ids)
    return [dict(r) for r in rows]


async def get_profile(viewer_id: int, target_id: int, clone_id: Optional[int]) -> dict:
    """Profile as `viewer_id` may see it. Hidden sections are None (not 0) so the UI can say 'private'."""
    own = viewer_id == target_id
    st = await get_settings(target_id, clone_id)
    stats = await get_stats(target_id, clone_id)
    out = {"user_id": target_id, "title_key": st["title_key"], "settings": st if own else None}
    show_col = own or st["show_collection"]
    out["stats"] = ({k: v for k, v in stats.items() if k != "trades"} if show_col else None)
    out["trades"] = stats["trades"] if (own or st["show_trades"]) else None
    out["coins"] = None
    if own or st["show_coins"]:
        pool = await _ce._pool()
        async with pool.acquire() as conn:
            out["coins"] = await conn.fetchval(
                "SELECT COALESCE((SELECT balance FROM card_coins WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2),0)",
                target_id, _clone_key(clone_id))
    out["featured"], out["signature"] = [], None
    if show_col and st["featured_copy_ids"]:
        pool = await _ce._pool()
        async with pool.acquire() as conn:
            cards = [c for c in await _card_view(conn, st["featured_copy_ids"])]
            owned = {r["copy_id"] for r in await conn.fetch(
                "SELECT copy_id FROM card_copies WHERE copy_id = ANY($1::bigint[]) AND owner_id=$2",
                st["featured_copy_ids"], target_id)}
        cards = [c for c in cards if c["copy_id"] in owned]
        out["featured"] = cards
        out["signature"] = next((c for c in cards if c["copy_id"] == st["signature_copy_id"]), None)
    return out


_SORTS = {"newest": "cp.acquired_at DESC", "strongest": f"{_STRENGTH_SQL} DESC", "rarest": f"{_RANK_SQL} DESC, cp.copy_id",
          "boosted": "cp.boost_level DESC, cp.copy_id"}


async def browse_collection(viewer_id: int, owner_id: int, clone_id: Optional[int], *, rarity: Optional[str] = None,
                            min_strength: int = 0, min_boost: int = 0, has_skills: bool = False,
                            tradeable_only: bool = False, sort: str = "newest", limit: int = 10, offset: int = 0) -> dict:
    """One page of an owner's cards (rentals included, flagged). Respects privacy for other viewers."""
    if sort not in _SORTS:
        raise StoreError("bad_sort", "Unknown sort.")
    if viewer_id != owner_id and not (await get_settings(owner_id, clone_id))["show_collection"]:
        raise StoreError("private", "This collection is private.")
    limit = max(1, min(limit, 25))
    where = ["cp.owner_id = $1", "COALESCE(cp.clone_id,-1) = $2", "cp.boost_level >= $3", f"{_STRENGTH_SQL} >= $4"]
    args: list = [owner_id, _clone_key(clone_id), min_boost, min_strength]
    if rarity:
        args.append(rarity); where.append(f"cc.rarity = ${len(args)}")
    if has_skills:
        where.append("jsonb_array_length(cc.skills) > 0")
    if tradeable_only:
        where.append("cc.tradeable AND NOT cp.trade_locked AND cp.rental_expires_at IS NULL")
    w = " AND ".join(where)
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM card_copies cp JOIN card_catalog cc USING (card_id) WHERE {w}", *args)
        rows = await conn.fetch(
            f"SELECT cp.copy_id, cp.card_id, cp.serial_no, cp.boost_level, cp.rental_expires_at, cp.trade_locked, "
            f"cc.name, cc.rarity, cc.stock_total, cc.tradeable, {_STRENGTH_SQL}::int AS strength, "
            f"COUNT(*) OVER (PARTITION BY cp.card_id) AS dup_count "
            f"FROM card_copies cp JOIN card_catalog cc USING (card_id) WHERE {w} ORDER BY {_SORTS[sort]} "
            f"LIMIT {limit} OFFSET {int(offset)}", *args)
    return {"total": total, "cards": [dict(r) | {"duplicate": r["dup_count"] > 1} for r in rows]}


async def check_achievements(user_id: int, clone_id: Optional[int]) -> list[str]:
    """Award any newly met achievements (idempotent). Returns the newly earned keys."""
    stats = await get_stats(user_id, clone_id)
    pool = await _ce._pool()
    new = []
    async with pool.acquire() as conn:
        for key, (_title, stat, need) in ACHIEVEMENTS.items():
            if stat == "completion_pct" and stats["catalogue_size"] == 0:
                continue
            if stats[stat] >= need:
                r = await conn.fetchval(
                    "INSERT INTO card_achievements (user_id, clone_id, key) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING "
                    "RETURNING key", user_id, clone_id, key)
                if r:
                    new.append(key)
    return new


async def list_achievements(user_id: int, clone_id: Optional[int]) -> list[dict]:
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, earned_at FROM card_achievements WHERE user_id=$1 "
                                "AND COALESCE(clone_id,-1)=$2 ORDER BY earned_at", user_id, _clone_key(clone_id))
    return [{"key": r["key"], "title": ACHIEVEMENTS[r["key"]][0], "earned_at": r["earned_at"]}
            for r in rows if r["key"] in ACHIEVEMENTS]


async def leaderboard(metric: str, clone_id: Optional[int], *, member_ids: Optional[Sequence[int]] = None,
                      limit: int = 10, offset: int = 0) -> list[dict]:
    """Global (member_ids=None) or local (the guild's member ids, supplied server-side). Permanent copies only."""
    limit = max(1, min(limit, 25))
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        if metric == "trades":
            rows = await conn.fetch(
                "SELECT u AS user_id, COUNT(*) AS score FROM (SELECT initiator_id AS u, clone_id FROM card_trades "
                "WHERE status='completed' UNION ALL SELECT recipient_id, clone_id FROM card_trades WHERE status='completed') t "
                "WHERE COALESCE(clone_id,-1)=$1 AND ($2::bigint[] IS NULL OR u = ANY($2)) "
                "GROUP BY u ORDER BY score DESC, u LIMIT $3 OFFSET $4",
                _clone_key(clone_id), list(member_ids) if member_ids is not None else None, limit, offset)
        elif metric in _METRICS:
            rows = await conn.fetch(
                f"SELECT cp.owner_id AS user_id, {_METRICS[metric]}::bigint AS score FROM card_copies cp "
                f"JOIN card_catalog cc USING (card_id) WHERE cp.rental_expires_at IS NULL AND COALESCE(cp.clone_id,-1)=$1 "
                f"AND ($2::bigint[] IS NULL OR cp.owner_id = ANY($2)) GROUP BY cp.owner_id "
                f"HAVING {_METRICS[metric]} > 0 ORDER BY score DESC, cp.owner_id LIMIT $3 OFFSET $4",
                _clone_key(clone_id), list(member_ids) if member_ids is not None else None, limit, offset)
        else:
            raise StoreError("bad_metric", "Unknown ranking.")
    return [{"rank": offset + i + 1, "user_id": r["user_id"], "score": r["score"]} for i, r in enumerate(rows)]
