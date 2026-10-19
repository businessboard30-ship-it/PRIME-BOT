"""Cat battle balance harness (fights plan, phase 2). Simulates many fights between stat builds and a balanced build of
equal Power Rating. Rule: no build above 60% against the balanced build. Run:  python scripts/cat_balance_harness.py [fights]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import cat_battle as cb  # noqa: E402
from modules import cat_stats as cs  # noqa: E402

LIMIT = 0.60
PAIRS = (("common", 0), ("uncommon", 5), ("rare", 12), ("legendary", 25))


def builds(points: int):
    """The balanced build plus one build per stat taken to the 60% cap (the rest spread over the others)."""
    cap = cs.stat_cap(points)
    out = {"balanced": cb.balanced_stats(points)}
    for s in cs.STATS:
        rest = points - cap
        others = [o for o in cs.STATS if o != s]
        b = {s: cap}
        for i, o in enumerate(others):
            b[o] = rest // 3 + (1 if i < rest % 3 else 0)
        out[f"{s}-heavy"] = b
    return out


def fighter(name: str, alloc: dict, rank: int) -> cb.Fighter:
    return cb.Fighter(name, alloc["strength"], alloc["guard"], alloc["speed"], alloc["spirit"], rank=rank,
                      skills=tuple(s.key for s in cs.skills_for(rank))[:cs.slots_for(rank)])


def run(fights: int = 2000):
    """{(rarity, rank, build): win share of that build vs balanced, with sides swapped half the time}."""
    res = {}
    for rarity, rank in PAIRS:
        points = cs.pool(rarity, rank)
        bal = fighter("balanced", builds(points)["balanced"], rank)
        for name, alloc in builds(points).items():
            if name == "balanced":
                continue
            f = fighter(name, alloc, rank)
            wins = 0
            for i in range(fights):
                if i % 2 == 0:
                    wins += cb.fight(f, bal, i).winner == "a"
                else:
                    wins += cb.fight(bal, f, i).winner == "b"
            res[(rarity, rank, name)] = wins / fights
    return res


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    worst = 0.0
    for (rarity, rank, name), share in run(n).items():
        worst = max(worst, share)
        print(f"{rarity:<9} rank {rank:>2}  {name:<15} {share:6.1%} {'FAIL' if share > LIMIT else ''}")
    print(f"worst: {worst:.1%} (limit {LIMIT:.0%})")
    sys.exit(1 if worst > LIMIT else 0)
