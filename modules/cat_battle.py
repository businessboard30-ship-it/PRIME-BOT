# path: modules/cat_battle.py

"""
Cat battle engine (play game only, never real money). Phase 2 of the fights plan. PURE: no database, no Discord.

Input: two fighter snapshots and a seed. Output: the round-by-round log and the winner. The same seed always gives the
same fight, so any result can be replayed and audited.

  HP          = 60 + 3 * (Guard + rank)
  Round order : higher Speed first; a tie is decided by a seeded coin
  Hit chance  = clamp(0.55 + SPEED_SWING * (share of the two Speeds - 0.5), 0.15, 0.90)
  Damage      = DAMAGE_BASE * (Str + 0.6 * Spirit) / (Str + 0.6 * Spirit + 0.6 * Guard_def + 1) * skill_mult * uniform(0.85, 1.15)
  Crit chance = min(0.30, 0.5 * Spirit share of the attacker's own points) (+0.15 with Lucky Paw); a crit does x1.75
  (Ratio forms give every stat diminishing returns, so no lopsided build beats a balanced one: tests/unit/test_cat_battle.py
  and scripts/cat_balance_harness.py hold the line at 60%.)
  Skills      : each action a ready skill fires if its condition is met (fixed priority list, no player input)
  Fight ends  : KO, or after 8 rounds the higher remaining HP % wins; an exact tie goes to the defender (side b)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from modules import cat_stats

MAX_ROUNDS = 8
HURT_FACTOR = 0.9                   # a Hurt cat fights at -10% stats
ITEM_CAP = 0.10                     # item bonus is capped at +10% of the rest of the Power Rating
ODDS_RUNS = 200

SKILL_PRIORITY = ("yarn_trap", "nap_heal", "fluff_guard", "zoomies_dodge", "counter_claw", "hiss", "lucky_paw", "pounce")
POUNCE_MULT = 1.8
HISS_MULT = 0.7
CRIT_MULT = 1.75
DAMAGE_BASE = 24.0
SPIRIT_POWER = 0.6              # Spirit also adds to attack power (skill power)
SPEED_SWING = 0.40
CRIT_SHARE, CRIT_MAX = 0.5, 0.30
LUCKY_CRIT = 0.15
COUNTER_SHARE = 0.30
HEAL_SHARE = 0.15

# a few lively marks for the replay (rarer emojis Discord shows)
E_HIT, E_CRIT, E_MISS, E_KO, E_WIN = "🪓", "🌠", "🫧", "🪦", "🏵️"


@dataclass(frozen=True)
class Fighter:
    name: str
    strength: int
    guard: int
    speed: int
    spirit: int
    rank: int = 0
    skills: Tuple[str, ...] = ()
    item_bonus: int = 0
    vet_pct: int = 0                # Veteran bonus from fights fought (cat_records), capped at +5

    @property
    def hp(self) -> int:
        return 60 + 3 * (self.guard + self.rank)

    @property
    def power(self) -> int:
        """Power Rating. The item bonus is capped at +10% of everything else."""
        rest = self.strength + self.guard + self.speed + self.spirit + 2 * len(self.skills)
        base = rest + min(int(self.item_bonus), int(rest * ITEM_CAP))
        return base + int(round(base * int(self.vet_pct) / 100)) if self.vet_pct else base


def from_cat(cat, stats: cat_stats.Stats, hurt: bool = False, item_bonus: int = 0, vet_pct: int = 0) -> Fighter:
    """A fighter snapshot of a cat: allocated points plus the free trait bonus; -10% if Hurt."""
    t = cat_stats.totals(stats, cat.trait)
    f = HURT_FACTOR if hurt else 1.0
    return Fighter(name=cat.name, strength=max(1, int(t["strength"] * f)), guard=int(t["guard"] * f),
                   speed=int(t["speed"] * f), spirit=int(t["spirit"] * f), rank=cat.cat_rank,
                   skills=tuple(k for k in stats.skills if k in cat_stats.SKILLS), item_bonus=item_bonus, vet_pct=int(vet_pct))


@dataclass
class _Side:
    f: Fighter
    hp: int
    cd: Dict[str, int] = field(default_factory=dict)
    block: bool = False             # Fluff Guard: the next hit is blocked
    dodge: bool = False             # Zoomies Dodge: the next hit misses
    counter: bool = False           # Counter Claw: part of the next hit is handed back
    weak: bool = False              # Hissed: -30% Strength on the next action
    skip: bool = False              # Yarn Trap: loses the next action
    healed: bool = False


@dataclass(frozen=True)
class BattleResult:
    winner: str                     # "a" or "b"
    rounds: Tuple[Tuple[str, ...], ...]     # one tuple of text lines per round
    hp: Tuple[int, int]
    max_hp: Tuple[int, int]
    seed: int
    ended_by: str                   # "ko" | "rounds"

    def log(self) -> List[str]:
        return [line for r in self.rounds for line in r]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _bar(hp: int, mx: int) -> str:
    n = 0 if hp <= 0 else max(1, round(8 * hp / mx))
    return "▰" * n + "▱" * (8 - n)


def _pick_skill(me: _Side, foe: _Side) -> Optional[str]:
    for key in SKILL_PRIORITY:
        if key not in me.f.skills or me.cd.get(key, 0) > 0:
            continue
        if key == "yarn_trap" and not foe.skip:
            return key
        if key == "nap_heal" and not me.healed and me.hp * 2 < me.f.hp:
            return key
        if key == "fluff_guard" and not me.block:
            return key
        if key == "zoomies_dodge" and not me.dodge:
            return key
        if key == "counter_claw" and not me.counter:
            return key
        if key == "hiss" and not foe.weak:
            return key
        if key in ("lucky_paw", "pounce"):
            return key
    return None


def _act(me: _Side, foe: _Side, rng: random.Random, lines: List[str]) -> None:
    n = me.f.name
    if me.skip:
        me.skip = False
        lines.append(f"🕸️ **{n}** is stuck in yarn and loses the turn!")
        return
    skill = _pick_skill(me, foe)
    mult, crit_bonus = 1.0, 0.0
    if skill:
        sk = cat_stats.SKILLS[skill]
        me.cd[skill] = sk.cooldown + 1          # counted down at the end of every round
        if skill == "yarn_trap":
            foe.skip = True
            lines.append(f"{sk.emoji} **{n}** throws a {sk.name}!")
        elif skill == "nap_heal":
            gain = max(1, int(me.f.hp * HEAL_SHARE))
            me.hp = min(me.f.hp, me.hp + gain)
            me.healed = True
            lines.append(f"{sk.emoji} **{n}** naps and heals {gain} HP.")
        elif skill == "fluff_guard":
            me.block = True
            lines.append(f"{sk.emoji} **{n}** fluffs up to block the next hit.")
        elif skill == "zoomies_dodge":
            me.dodge = True
            lines.append(f"{sk.emoji} **{n}** gets the zoomies and will dodge the next hit.")
        elif skill == "counter_claw":
            me.counter = True
            lines.append(f"{sk.emoji} **{n}** readies a {sk.name}.")
        elif skill == "hiss":
            foe.weak = True
            lines.append(f"{sk.emoji} **{n}** hisses and {foe.f.name} gets shaky.")
        elif skill == "lucky_paw":
            crit_bonus = LUCKY_CRIT
            lines.append(f"{sk.emoji} **{n}** rubs a {sk.name}.")
        elif skill == "pounce":
            mult = POUNCE_MULT
            lines.append(f"{sk.emoji} **{n}** pounces!")
    strength = (me.f.strength + SPIRIT_POWER * me.f.spirit) * (HISS_MULT if me.weak else 1.0)
    if me.f.vet_pct:
        strength *= 1 + me.f.vet_pct / 100
    me.weak = False
    sp = me.f.speed + foe.f.speed
    hit = _clamp(0.55 + SPEED_SWING * ((me.f.speed / sp if sp else 0.5) - 0.5), 0.15, 0.90)
    if foe.dodge:
        foe.dodge = False
        lines.append(f"{E_MISS} **{foe.f.name}** dodges **{n}**'s attack.")
        return
    if rng.random() >= hit:
        lines.append(f"{E_MISS} **{n}** misses.")
        return
    dmg = DAMAGE_BASE * strength / (strength + 0.6 * foe.f.guard + 1) * mult * rng.uniform(0.85, 1.15)
    own = me.f.strength + me.f.guard + me.f.speed + me.f.spirit
    crit = rng.random() < min(CRIT_MAX, CRIT_SHARE * (me.f.spirit / own if own else 0)) + crit_bonus
    if crit:
        dmg *= CRIT_MULT
    dmg_i = max(1, int(round(dmg)))
    if foe.block:
        foe.block = False
        lines.append(f"☁️ **{foe.f.name}**'s fluff blocks the hit!")
        return
    foe.hp = max(0, foe.hp - dmg_i)
    lines.append(f"{E_CRIT if crit else E_HIT} **{n}** hits **{foe.f.name}** for **{dmg_i}**{' (crit!)' if crit else ''}.")
    if foe.counter and foe.hp > 0:
        foe.counter = False
        back = max(1, int(dmg_i * COUNTER_SHARE))
        me.hp = max(0, me.hp - back)
        lines.append(f"🪝 **{foe.f.name}** claws back for {back}.")


def fight(a: Fighter, b: Fighter, seed: int) -> BattleResult:
    """Run a whole fight. Side a is the challenger, side b the defender (wins an exact tie)."""
    rng = random.Random(seed)
    sa, sb = _Side(a, a.hp), _Side(b, b.hp)
    order = [sa, sb]
    if b.speed > a.speed or (a.speed == b.speed and rng.random() < 0.5):
        order = [sb, sa]
    rounds: List[Tuple[str, ...]] = []
    ended = "rounds"
    for n in range(1, MAX_ROUNDS + 1):
        lines = [f"**Round {n}**"]
        for me in order:
            foe = sb if me is sa else sa
            _act(me, foe, rng, lines)
            if sa.hp <= 0 or sb.hp <= 0:
                break
        lines.append(f"{_bar(sa.hp, a.hp)} {a.name} {sa.hp}/{a.hp}  ·  {b.name} {sb.hp}/{b.hp} {_bar(sb.hp, b.hp)}")
        rounds.append(tuple(lines))
        if sa.hp <= 0 or sb.hp <= 0:
            ended = "ko"
            break
        for s in (sa, sb):
            s.cd = {k: v - 1 for k, v in s.cd.items() if v > 1}
    if ended == "ko":
        winner = "b" if sa.hp <= 0 and sb.hp > 0 else "a" if sb.hp <= 0 and sa.hp > 0 else "b"
    else:
        pa, pb = sa.hp / a.hp, sb.hp / b.hp
        winner = "a" if pa > pb else "b"
    return BattleResult(winner, tuple(rounds), (sa.hp, sb.hp), (a.hp, b.hp), seed, ended)


# ── odds bands ──────────────────────────────────────────────────────────

BANDS = ((0.35, "Long shot", "🪫"), (0.65, "Even", "⚖️"), (0.85, "Favoured", "🧭"), (1.01, "Heavy favourite", "🏰"))


def win_share(a: Fighter, b: Fighter, seed: int = 1, runs: int = ODDS_RUNS) -> float:
    return sum(fight(a, b, seed * 100_003 + i).winner == "a" for i in range(runs)) / runs


def band(share: float) -> Tuple[str, str]:
    """(label, emoji). Bands, not exact percentages, but honest."""
    for top, label, emoji in BANDS:
        if share < top:
            return label, emoji
    return BANDS[-1][1], BANDS[-1][2]


def odds(a: Fighter, b: Fighter, seed: int = 1) -> Tuple[str, str]:
    return band(win_share(a, b, seed))


def within_band(p1: int, p2: int, share: float = 0.30) -> bool:
    """Matching later uses +-30% Power Rating (open board and league)."""
    lo, hi = sorted((max(1, p1), max(1, p2)))
    return hi <= lo * (1 + share)


# ── NPC opponents (Practice) ────────────────────────────────────────────

NPCS = (("Pebble the Practice Cat", "🪨"), ("Mossy Whiskers", "🪴"), ("Captain Crumb", "🥐"), ("Sir Fluffington", "🪭"),
        ("Madame Marble", "🫐"), ("Zig Zag Zed", "🪁"))
DIFFICULTY = {"easy": ("Easy", "🌱", 0.75), "normal": ("Normal", "🌿", 1.0), "hard": ("Hard", "🌳", 1.25)}


def balanced_stats(points: int) -> Dict[str, int]:
    """Spread points evenly over the four stats (the remainder goes Strength, Guard, Speed, Spirit in turn)."""
    base, extra = divmod(max(0, points), 4)
    return {s: base + (1 if i < extra else 0) for i, s in enumerate(cat_stats.STATS)}


def make_npc(cat_pool_points: int, rank: int, level: str, seed: int) -> Fighter:
    """A balanced practice opponent sized to the player's cat. `level` scales its points: easy x0.75, hard x1.25."""
    key = level if level in DIFFICULTY else "normal"
    rng = random.Random(seed)
    name, emoji = NPCS[rng.randrange(len(NPCS))]
    points = max(4, int(round(cat_pool_points * DIFFICULTY[key][2])))
    st = balanced_stats(points)
    skills = tuple(s.key for s in cat_stats.skills_for(rank))[:cat_stats.slots_for(rank)]
    return Fighter(f"{emoji} {name}", st["strength"], st["guard"], st["speed"], st["spirit"], rank=rank, skills=skills)
