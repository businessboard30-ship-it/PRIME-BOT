# path: modules/cat_practice.py

"""
Practice fights against an NPC cat (play game only, never real money). Phase 2 of the fights plan.

No stakes and nothing is risked. A win pays a SMALL coin reward (the only new faucet in this phase), with a short
cooldown and a daily cap on rewarded wins. The cap and cooldown are kept in memory (they reset when the bot restarts,
which can only ever give a few extra small rewards; Phase 3 moves the fight limits into the database).
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from modules import cat_battle, cat_stats, cat_wallet

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 45
BASE_REWARD = 25                    # coins for a Normal win by a common cat, before the multipliers
LEVEL_REWARD = {"easy": 0.5, "normal": 1.0, "hard": 1.5}
DAILY_REWARDED_WINS = 10

_last: Dict[int, float] = {}
_daily: Dict[Tuple[int, str], int] = {}


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def cooldown_left(owner_id: int, now: Optional[float] = None) -> int:
    """Seconds until this person may start another practice fight (0 = go ahead)."""
    t = (now if now is not None else time.monotonic()) - _last.get(owner_id, -1e9)
    return max(0, math.ceil(COOLDOWN_SECONDS - t))


def wins_today(owner_id: int, now: Optional[datetime] = None) -> int:
    return _daily.get((owner_id, _today(now)), 0)


def reward_for(rarity: str, level: str) -> int:
    return max(1, int(round(BASE_REWARD * cat_wallet.RARITY_MULTIPLIER.get(rarity, 1.0) * LEVEL_REWARD.get(level, 1.0))))


@dataclass(frozen=True)
class Practice:
    mine: cat_battle.Fighter
    npc: cat_battle.Fighter
    level: str
    result: cat_battle.BattleResult
    reward: int                     # coins that will be paid (0 = none)

    @property
    def won(self) -> bool:
        return self.result.winner == "a"


def odds_for(cat, stats: cat_stats.Stats, level: str, seed: int = 1) -> Tuple[str, str]:
    """The odds band against this level of NPC (200 quick simulations)."""
    mine = cat_battle.from_cat(cat, stats)
    npc = cat_battle.make_npc(cat_stats.pool(cat.rarity, cat.cat_rank), cat.cat_rank, level, seed)
    return cat_battle.odds(mine, npc, seed)


def play(cat, stats: cat_stats.Stats, level: str, owner_id: int, now: Optional[datetime] = None) -> Practice:
    """Run one practice fight. Pure apart from the seed; the reward is decided here and paid by `pay`."""
    level = level if level in cat_battle.DIFFICULTY else "normal"
    seed = secrets.randbelow(2 ** 31)
    mine = cat_battle.from_cat(cat, stats)
    npc = cat_battle.make_npc(cat_stats.pool(cat.rarity, cat.cat_rank), cat.cat_rank, level, seed)
    result = cat_battle.fight(mine, npc, seed)
    reward = 0
    if result.winner == "a" and wins_today(owner_id, now) < DAILY_REWARDED_WINS:
        reward = reward_for(cat.rarity, level)
    return Practice(mine, npc, level, result, reward)


def start(owner_id: int, now: Optional[float] = None) -> None:
    _last[owner_id] = now if now is not None else time.monotonic()


async def pay(owner_id: int, clone_id, p: Practice, now: Optional[datetime] = None) -> int:
    """Pay the reward in one transaction and count it. Returns the coins paid (0 when there is none or it failed)."""
    if p.reward <= 0:
        return 0
    key = (owner_id, _today(now))
    if _daily.get(key, 0) >= DAILY_REWARDED_WINS:
        return 0
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await cat_wallet.add_reward(conn, owner_id, clone_id, cat_wallet.Reward("coins", p.reward))
    except Exception:
        logger.exception("[cat-practice] couldn't pay the practice reward")
        return 0
    _daily[key] = _daily.get(key, 0) + 1
    return p.reward
