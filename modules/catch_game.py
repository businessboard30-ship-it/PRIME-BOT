"""Catch game rules that never touch Discord or the database (P0-01/02/03).

Everything here is a pure function or a constant so it can be unit-tested
and reused by every source (wild spawns, encounters, fishing, eggs, ...).
The documented versions of these numbers live in docs/catch/GAME_DESIGN.md;
change both together.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

STAT_NAMES = ("vigor", "power", "guard", "speed", "spirit")
IV_MAX = 31
LEVEL_MIN, LEVEL_MAX = 1, 100

ELEMENTS = ("ember", "tide", "verdant", "volt", "frost", "stone", "gale", "umbra", "lumen")
HABITATS = ("land", "water", "forest", "cave", "sky")
EXCLUSIVE_SOURCES = ("fishing", "egg", "lottery", "event", "vote")


@dataclass(frozen=True)
class Rarity:
    code: int
    key: str
    spawn_weight_bp: int
    base_catch_rate: float
    level_range: tuple[int, int]
    flee_chance: float
    sell_value: int


# spawn_weight_bp sums to 10000. "special" is a variant tier (P0-01), never a
# wild roll, so it is not in this table.
RARITIES: tuple[Rarity, ...] = (
    Rarity(0, "common", 6000, 0.70, (2, 15), 0.05, 10),
    Rarity(1, "uncommon", 2600, 0.55, (8, 22), 0.10, 30),
    Rarity(2, "rare", 1000, 0.40, (15, 32), 0.20, 120),
    Rarity(3, "epic", 330, 0.25, (25, 45), 0.30, 500),
    Rarity(4, "mythic", 70, 0.12, (40, 60), 0.45, 2500),
)
RARITY_BY_KEY = {r.key: r for r in RARITIES}
RARITY_BY_CODE = {r.code: r for r in RARITIES}

SHINY_ODDS = 512
SHINY_SELL_MULTIPLIER = 5
SPECIAL_SELL_MULTIPLIER = 10

# Ball items (P0-03 name sheet). Multiplier applies to the catch chance.
BALLS: dict[str, float] = {
    "capsule_basic": 1.0,
    "capsule_sturdy": 1.5,
    "capsule_prime": 2.0,
    "capsule_sovereign": 255.0,
}
BAIT_BONUS: dict[str, float] = {"honeyberry": 0.10, "goldberry": 0.20}
MIN_CATCH_CHANCE = 0.01
MAX_CATCH_CHANCE = 0.95

# Source codes stored in catch_owned.source (SMALLINT). Never renumber.
SOURCES: dict[str, int] = {
    "wild": 0,
    "encounter": 1,
    "fishing": 2,
    "catchbot": 3,
    "egg": 4,
    "lootbox": 5,
    "swap": 6,
    "lottery": 7,
    "giveaway": 8,
    "drop": 9,
    "vote": 10,
    "event": 11,
    "admin_grant": 12,
}
SOURCE_BY_CODE = {v: k for k, v in SOURCES.items()}


def rarity(key_or_code: str | int) -> Rarity:
    if isinstance(key_or_code, int):
        return RARITY_BY_CODE[key_or_code]
    return RARITY_BY_KEY[key_or_code]


def roll_rarity(rng: random.Random, *, boost_bp: dict[str, int] | None = None) -> Rarity:
    """Weighted rarity roll. ``boost_bp`` adds basis points to named tiers
    (events, lures) and the common tier absorbs the difference."""
    weights = {r.key: r.spawn_weight_bp for r in RARITIES}
    for key, extra in (boost_bp or {}).items():
        weights[key] = max(0, weights[key] + extra)
    weights["common"] = max(0, 10000 - sum(v for k, v in weights.items() if k != "common"))
    pick = rng.randrange(sum(weights.values()))
    for r in RARITIES:
        pick -= weights[r.key]
        if pick < 0:
            return r
    return RARITIES[0]


def roll_level(rng: random.Random, tier: Rarity) -> int:
    low, high = tier.level_range
    return rng.randint(low, high)


def roll_ivs(rng: random.Random) -> list[int]:
    return [rng.randint(0, IV_MAX) for _ in STAT_NAMES]


def roll_shiny(rng: random.Random, odds: int = SHINY_ODDS) -> bool:
    return rng.randrange(max(1, odds)) == 0


def compute_stats(base: list[int], ivs: list[int], level: int) -> dict[str, int]:
    """Stat formula (P0-02). Vigor is the HP-like stat and grows with level.

    vigor = floor((2*base + iv) * level / 100) + level + 10
    other = floor((2*base + iv) * level / 100) + 5
    """
    if len(base) != len(STAT_NAMES) or len(ivs) != len(STAT_NAMES):
        raise ValueError("base and ivs need one value per stat")
    level = max(LEVEL_MIN, min(LEVEL_MAX, level))
    out: dict[str, int] = {}
    for i, name in enumerate(STAT_NAMES):
        core = (2 * base[i] + ivs[i]) * level // 100
        out[name] = core + (level + 10 if name == "vigor" else 5)
    return out


def iv_percent(ivs: list[int]) -> float:
    return round(100 * sum(ivs) / (IV_MAX * len(STAT_NAMES)), 1)


def catch_chance(
    tier: Rarity,
    *,
    ball: str = "capsule_basic",
    level: int = 1,
    species_mod: float = 1.0,
    bait: str | None = None,
    streak: int = 0,
    shiny: bool = False,
) -> float:
    """Catch-rate formula (P0-02).

    chance = base_rate(rarity) * species_mod * ball_mult * level_factor
             + bait_bonus + streak_bonus,   then clamped to [0.01, 0.95]
    level_factor  = 1 - (level - 1) / 300        (L1 = 1.0, L100 = 0.67)
    streak_bonus  = min(streak, 20) * 0.005      (max +10%)
    shiny         = chance * 0.8
    The top ball (multiplier >= 255) always succeeds.
    """
    mult = BALLS.get(ball)
    if mult is None:
        raise ValueError(f"unknown ball: {ball}")
    if mult >= 255:
        return 1.0
    level_factor = 1 - (max(LEVEL_MIN, min(LEVEL_MAX, level)) - 1) / 300
    chance = tier.base_catch_rate * species_mod * mult * level_factor
    chance += BAIT_BONUS.get(bait or "", 0.0)
    chance += min(max(streak, 0), 20) * 0.005
    if shiny:
        chance *= 0.8
    return round(max(MIN_CATCH_CHANCE, min(MAX_CATCH_CHANCE, chance)), 4)


def sell_value(tier: Rarity, *, shiny: bool = False, special: bool = False) -> int:
    value = tier.sell_value
    if special:
        value *= SPECIAL_SELL_MULTIPLIER
    elif shiny:
        value *= SHINY_SELL_MULTIPLIER
    return value


def weighted_pick(rng: random.Random, weights_bp: dict[str, int]) -> str:
    """Pick a key from a basis-point table (drop tables, eggs, swap)."""
    total = sum(weights_bp.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    pick = rng.randrange(total)
    for key, weight in weights_bp.items():
        pick -= weight
        if pick < 0:
            return key
    return next(iter(weights_bp))
