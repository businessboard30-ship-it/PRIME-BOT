# path: modules/cat_board.py

"""
Cat leaderboards (play game only). Counts every cat a person owns across the main bot and all clones.

  * Collectors: rarity points (common 1, uncommon 3, rare 8, legendary 25), then number of cats.
  * Richest: coins in their wallets (a diamond counts as 10,000 coins), summed across bots.
"""

from __future__ import annotations

from typing import List, Optional

from modules import cat_wallet

POINTS = {"common": 1, "uncommon": 3, "rare": 8, "legendary": 25}
MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")

_POINTS_SQL = ("CASE rarity WHEN 'legendary' THEN 25 WHEN 'rare' THEN 8 WHEN 'uncommon' THEN 3 ELSE 1 END")
_COLLECTORS = (f"SELECT owner_id, COUNT(*) AS cats, SUM({_POINTS_SQL}) AS score, "
               "COUNT(*) FILTER (WHERE rarity = 'legendary') AS legendary FROM cat_adoptions GROUP BY owner_id")
_RICHEST = ("SELECT owner_id, SUM(coins + diamonds::bigint * $1) AS score FROM cat_wallets GROUP BY owner_id "
            "HAVING SUM(coins + diamonds::bigint * $1) > 0")


async def _pool():
    from database import get_pool
    return await get_pool()


async def top_collectors(limit: int = 10, me: Optional[int] = None) -> tuple:
    """(rows, my_rank_or_None); each row is a dict with owner_id, cats, score, legendary."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM ({_COLLECTORS}) t ORDER BY score DESC, cats DESC, owner_id LIMIT $1", limit)
        rank = None
        if me is not None:
            rank = await conn.fetchval(
                f"SELECT r FROM (SELECT owner_id, RANK() OVER (ORDER BY score DESC, cats DESC) AS r FROM ({_COLLECTORS}) t) x "
                "WHERE owner_id = $1", me)
    return [dict(r) for r in rows], (int(rank) if rank else None)


async def top_richest(limit: int = 10, me: Optional[int] = None) -> tuple:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM ({_RICHEST}) t ORDER BY score DESC, owner_id LIMIT $2",
                                cat_wallet.DIAMOND_COINS, limit)
        rank = None
        if me is not None:
            rank = await conn.fetchval(
                f"SELECT r FROM (SELECT owner_id, RANK() OVER (ORDER BY score DESC) AS r FROM ({_RICHEST}) t) x "
                "WHERE owner_id = $2", cat_wallet.DIAMOND_COINS, me)
    return [dict(r) for r in rows], (int(rank) if rank else None)


def format_board(rows: List[dict], mode: str, my_rank: Optional[int] = None) -> str:
    """The text of one leaderboard. Names are mentions, which show the name and never ping inside an embed."""
    if not rows:
        return "Nobody is on the board yet. Adopt a cat to be the first!"
    lines = []
    for i, r in enumerate(rows):
        place = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
        if mode == "richest":
            detail = f"{int(r['score']):,} coins"
        else:
            leg = int(r.get("legendary") or 0)
            detail = f"{int(r['cats'])} cats \u00b7 {int(r['score']):,} pts" + (f" \u00b7 {leg} legendary" if leg else "")
        lines.append(f"{place} <@{int(r['owner_id'])}> \u2014 {detail}")
    if my_rank and my_rank > len(rows):
        lines.append(f"\n**You:** #{my_rank}")
    return "\n".join(lines)
