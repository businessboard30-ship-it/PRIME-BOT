"""Cat battle engine, practice fights and balance harness (fights plan, phase 2)."""
import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import discord
import pytest

from discord_bot.cogs import _views_cat_fight as vf
from discord_bot.cogs import _views_cat_stats as vs
from modules import cat_adopt as ca
from modules import cat_battle as cb
from modules import cat_practice as cp
from modules import cat_stats as cs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def F(name="A", s=5, g=5, sp=5, sx=5, rank=0, skills=(), item=0):
    return cb.Fighter(name, s, g, sp, sx, rank=rank, skills=tuple(skills), item_bonus=item)


def cat(rank=0, rarity="common", trait="Brave"):
    return ca.Cat(id=7, owner_id=1, clone_id=None, pool_id=9, name="Whiskers", rarity=rarity, trait=trait, cat_rank=rank)


# ---------- determinism ----------
def test_same_seed_same_fight_different_seed_can_differ():
    a, b = F("A", 8, 3, 4, 5, skills=("pounce", "hiss")), F("B", 4, 6, 5, 5, skills=("fluff_guard",))
    r1, r2 = cb.fight(a, b, 42), cb.fight(a, b, 42)
    assert r1 == r2 and r1.log() == r2.log()
    assert len({tuple(cb.fight(a, b, s).log()) for s in range(20)}) > 1


def test_every_fight_ends_with_a_winner_inside_eight_rounds():
    for seed in range(200):
        r = cb.fight(F("A", 9, 2, 5, 4, skills=("yarn_trap", "nap_heal")), F("B", 3, 8, 3, 6, skills=("counter_claw",)), seed)
        assert r.winner in ("a", "b") and 1 <= len(r.rounds) <= cb.MAX_ROUNDS
        assert r.ended_by in ("ko", "rounds") and (r.ended_by == "rounds") == (len(r.rounds) == cb.MAX_ROUNDS and min(r.hp) > 0)


# ---------- formulas ----------
def test_hp_and_power_rating():
    f = F("A", 4, 5, 3, 2, rank=10, skills=("pounce", "hiss"))
    assert f.hp == 60 + 3 * (5 + 10) and f.power == 4 + 5 + 3 + 2 + 4


def test_item_bonus_is_capped_at_ten_percent_of_the_rest():
    base = F("A", 10, 10, 10, 10)                       # rest = 40
    assert F("A", 10, 10, 10, 10, item=2).power == 42
    assert F("A", 10, 10, 10, 10, item=99).power == 44 and base.power == 40


def test_hurt_cats_fight_at_minus_ten_percent_and_trait_bonus_counts():
    st = cs.Stats(strength=10, guard=10, speed=10, spirit=10)
    ok, hurt = cb.from_cat(cat(), st), cb.from_cat(cat(), st, hurt=True)
    assert ok.strength == 11 and ok.guard == 10                        # Brave: +1 Strength
    assert hurt.strength == 9 and hurt.guard == 9 and hurt.speed == 9


def test_faster_cat_acts_first_and_a_speed_tie_is_a_seeded_coin():
    r = cb.fight(F("Slow", sp=1), F("Fast", sp=9), 1)
    assert "Fast" in r.rounds[0][1]
    firsts = {("A" in cb.fight(F("A"), F("B"), s).rounds[0][1].split("**")[1]) for s in range(30)}
    assert firsts == {True, False}


def test_exact_hp_tie_goes_to_the_defender(monkeypatch):
    monkeypatch.setattr(cb, "_act", lambda me, foe, rng, lines: None)       # nobody hurts anybody: an exact tie
    r = cb.fight(F("A"), F("B"), 1)
    assert r.hp[0] == r.max_hp[0] and r.hp[1] == r.max_hp[1] and r.winner == "b" and r.ended_by == "rounds"


# ---------- skills ----------
def _log(a, b, seeds=40):
    return "\n".join("\n".join(cb.fight(a, b, s).log()) for s in range(seeds))


def test_skills_show_up_in_the_log():
    for key, word in (("pounce", "pounces"), ("hiss", "hisses"), ("fluff_guard", "fluffs up"), ("zoomies_dodge", "zoomies"),
                      ("lucky_paw", "Lucky Paw"), ("counter_claw", "Counter Claw"), ("yarn_trap", "Yarn Trap")):
        assert word in _log(F("A", 6, 3, 5, 5, skills=(key,)), F("B", 5, 5, 5, 5)), key
    assert "heals" in _log(F("A", 2, 2, 5, 5, skills=("nap_heal",)), F("B", 12, 2, 5, 5))


def test_cooldown_stops_a_skill_firing_every_round():
    r = cb.fight(F("A", 1, 20, 5, 0, skills=("pounce",)), F("B", 1, 20, 5, 0), 3)
    casts = [i for i, rd in enumerate(r.rounds) if any("pounces" in ln for ln in rd)]
    assert len(casts) >= 2 and all(b - a >= cs.SKILLS["pounce"].cooldown + 1 for a, b in zip(casts, casts[1:]))


def test_yarn_trap_makes_the_other_cat_lose_a_turn():
    assert "loses the turn" in _log(F("A", 6, 5, 9, 5, skills=("yarn_trap",)), F("B", 5, 5, 5, 5))


# ---------- odds ----------
def test_odds_bands_are_ordered_and_sensible():
    assert [cb.band(x)[0] for x in (0.1, 0.5, 0.75, 0.95)] == ["Long shot", "Even", "Favoured", "Heavy favourite"]
    big, small = F("Big", 12, 12, 12, 12), F("Small", 3, 3, 3, 3)
    assert cb.odds(big, small)[0] == "Heavy favourite" and cb.odds(small, big)[0] == "Long shot"
    assert cb.odds(F("A"), F("B"))[0] in ("Long shot", "Even")        # equal fighters: defender wins ties


def test_power_band_matching():
    assert cb.within_band(100, 130) and not cb.within_band(100, 131) and cb.within_band(130, 100)


# ---------- NPCs ----------
def test_npc_scales_with_level_and_is_deterministic():
    e, n, h = (cb.make_npc(20, 5, lv, 3) for lv in ("easy", "normal", "hard"))
    assert e.power < n.power < h.power and cb.make_npc(20, 5, "normal", 3) == n
    assert cb.make_npc(20, 5, "bogus", 3) == n and len(n.skills) == cs.slots_for(5)


# ---------- balance harness ----------
def _harness():
    spec = importlib.util.spec_from_file_location("cat_balance_harness", ROOT / "scripts/cat_balance_harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_stat_build_beats_a_balanced_build_by_more_than_sixty_percent():
    h = _harness()
    results = h.run(600)
    assert len(results) == 16
    worst = max(results.values())
    assert worst <= h.LIMIT + 0.05, f"worst build wins {worst:.1%} (limit {h.LIMIT:.0%} + 5% sampling tolerance)"


# ---------- practice ----------
def test_rewards_scale_with_rarity_and_level():
    assert cp.reward_for("common", "normal") == 25 and cp.reward_for("legendary", "hard") == 188
    assert cp.reward_for("common", "easy") < cp.reward_for("common", "hard")


def test_cooldown_and_daily_cap(monkeypatch):
    cp._last.clear(); cp._daily.clear()
    assert cp.cooldown_left(5, now=1000.0) == 0
    cp.start(5, now=1000.0)
    assert cp.cooldown_left(5, now=1010.0) == 35 and cp.cooldown_left(5, now=1045.0) == 0
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    cp._daily[(5, "2026-10-19")] = cp.DAILY_REWARDED_WINS
    wins = [cp.play(cat(), cs.Stats(strength=4, guard=2, speed=1, spirit=1), "easy", 5, now) for _ in range(40)]
    assert any(p.won for p in wins) and all(p.reward == 0 for p in wins)       # capped: still fun, no coins


def test_a_loss_pays_nothing_and_a_win_is_paid_once(monkeypatch):
    cp._daily.clear()
    paid_calls = []

    class Conn:
        def transaction(self):
            class T:
                async def __aenter__(s): return None
                async def __aexit__(s, *a): return False
            return T()

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s): return Conn()
                async def __aexit__(s, *a): return False
            return A()

    async def get_pool():
        return Pool()

    async def add_reward(conn, owner, clone, reward):
        paid_calls.append((owner, reward.kind, reward.amount))

    import database
    monkeypatch.setattr(database, "get_pool", get_pool)
    monkeypatch.setattr(cp.cat_wallet, "add_reward", add_reward)
    st = cs.Stats(strength=4, guard=2, speed=1, spirit=1)
    won = next(p for p in (cp.play(cat(), st, "easy", 9) for _ in range(100)) if p.won)
    lost = next(p for p in (cp.play(cat(), cs.Stats(), "hard", 9) for _ in range(100)) if not p.won)
    assert run(cp.pay(9, None, lost)) == 0 and paid_calls == []
    assert run(cp.pay(9, None, won)) == won.reward and paid_calls == [(9, "coins", won.reward)]
    assert cp.wins_today(9) == 1


# ---------- screens ----------
def _count(view):
    return sum(1 for _ in view.walk_children())


class Parent:
    user = type("U", (), {"id": 1})()

    async def interaction_check(self, i):
        return True


@pytest.mark.parametrize("rank,rarity", [(0, "common"), (12, "rare"), (25, "legendary")])
def test_practice_screens_fit_the_budget(rank, rarity):
    c, st = cat(rank, rarity), cs.Stats(strength=3, skills=tuple(s.key for s in cs.skills_for(rank))[:cs.slots_for(rank)])
    odds = {k: ("Heavy favourite", "🏰") for k in cb.DIFFICULTY}
    sv = vs.StatsView(Parent(), c, st)
    pv = vf.PracticeView(sv, c, st, odds, note="x" * 100)
    assert _count(pv) <= 40 and _count(sv) <= 40
    p = cp.play(c, st, "hard", 1)
    for n in range(1, len(p.result.rounds) + 1):
        v = vf._screen(vf.replay_text(p, n))
        assert _count(v) <= 40 and len(vf.replay_text(p, n)) < 4000
    assert len(vf.result_text(p, 100)) < 4000


def test_result_text_names_the_winner_and_the_reward():
    p = cp.play(cat(), cs.Stats(strength=4, guard=2, speed=1, spirit=1), "easy", 1)
    t = vf.result_text(p, 25)
    assert "wins" in t and "+25" in t and str(p.result.seed) in t


def test_stats_screen_has_a_practice_button_that_goes_to_the_arena():
    labels = [getattr(b, "label", None) for b in vs.StatsView(Parent(), cat(), cs.Stats()).walk_children()]
    assert "Practice" in labels
    assert "open_practice" in (ROOT / "discord_bot/cogs/_views_cat_stats.py").read_text()


def test_no_database_or_discord_in_the_engine():
    src = (ROOT / "modules/cat_battle.py").read_text()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert not any(w in ln for ln in imports for w in ("discord", "database", "asyncpg"))
