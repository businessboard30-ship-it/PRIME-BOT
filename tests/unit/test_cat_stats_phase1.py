"""Cat stats and skills (fights plan, phase 1): pure rules, transactions against an in-memory fake, screen budget, wiring."""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_cat_stats as vs
from modules import cat_adopt as ca
from modules import cat_stats as cs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def cat(rank=0, rarity="common", trait="Brave", cid=7):
    return ca.Cat(id=cid, owner_id=1, clone_id=None, pool_id=9, name="Whiskers", rarity=rarity, trait=trait, cat_rank=rank)


# ---------- pure rules ----------
def test_pool_is_rarity_base_plus_two_per_rank():
    assert [cs.pool(r, 0) for r in ("common", "uncommon", "rare", "legendary")] == [8, 10, 13, 16]
    assert cs.pool("legendary", 25) == 66                     # the plan's example
    assert cs.pool("weird", 99) == 8 + 50                     # unknown rarity = common, rank clamped to 25


def test_cap_is_sixty_percent_rounded_down():
    assert cs.stat_cap(66) == 39 and cs.stat_cap(8) == 4 and cs.stat_cap(10) == 6


def test_trait_bonus_is_a_free_plus_one_and_others_get_none():
    assert cs.trait_bonus("Brave")["strength"] == 1 and cs.trait_bonus("Zoomies")["speed"] == 1
    assert cs.trait_bonus("Chonky")["guard"] == 1 and cs.trait_bonus("Gentle")["guard"] == 1
    assert cs.trait_bonus("Dreamy")["spirit"] == 1 and cs.trait_bonus("Sassy")["spirit"] == 1
    assert sum(cs.trait_bonus("Sleepy").values()) == 0
    st = cs.Stats(strength=2)
    assert cs.totals(st, "Brave")["strength"] == 3 and cs.totals(st, "Sleepy")["strength"] == 2


def test_respec_is_free_once_then_costs_by_rank():
    assert cs.respec_cost(10, 0) == 0
    assert cs.respec_cost(0, 1) == 10_000 and cs.respec_cost(10, 3) == 30_000 and cs.respec_cost(25, 1) == 60_000


def test_slots_and_skill_unlocks_follow_rank():
    assert [cs.slots_for(r) for r in (0, 4, 5, 11, 12, 25)] == [1, 1, 2, 2, 3, 3]
    assert {s.key for s in cs.skills_for(0)} == {"pounce", "hiss", "fluff_guard"}
    assert "yarn_trap" not in {s.key for s in cs.skills_for(11)} and "yarn_trap" in {s.key for s in cs.skills_for(12)}
    assert len(cs.SKILLS) == 8 and all(s.cooldown > 0 for s in cs.SKILLS.values())


def test_allocation_rules():
    old = {s: 0 for s in cs.STATS}
    ok = {"strength": 4, "guard": 4, "speed": 0, "spirit": 0}
    assert cs.validate_alloc(ok, old, "common", 0) is None                       # 8 points, cap 4
    assert cs.validate_alloc({**ok, "strength": 5}, old, "common", 0) == "cap"   # above 60%
    assert cs.validate_alloc({**ok, "speed": 1}, old, "common", 0) == "pool"     # 9 > 8
    assert cs.validate_alloc({**ok, "guard": -1}, old, "common", 0) == "invalid"
    assert cs.validate_alloc({**ok, "guard": True}, old, "common", 0) == "invalid"
    assert cs.validate_alloc({"strength": 1, "guard": 0, "speed": 0, "spirit": 0},
                             {**old, "strength": 3}, "common", 0) == "shrink"    # taking points back is a respec
    assert cs.validate_alloc({"strength": 1}, old, "common", 0) == "invalid"     # a missing stat


def test_skill_choice_rules():
    assert cs.validate_skills(["pounce"], 0) is None
    assert cs.validate_skills(["pounce", "hiss"], 0) == "slots"
    assert cs.validate_skills(["pounce", "pounce"], 5) == "dupe"
    assert cs.validate_skills(["nap_heal"], 0) == "rank" and cs.validate_skills(["nope"], 0) == "unknown"
    assert cs.validate_skills([], 0) is None and cs.validate_skills(["yarn_trap", "hiss", "pounce"], 12) is None


def test_power_rating_counts_stats_trait_and_skills():
    st = cs.Stats(strength=3, guard=2, skills=("pounce", "hiss"))
    assert cs.power_rating(st, "Brave") == 3 + 1 + 2 + 4 and cs.unspent(st, "common", 0) == 3


# ---------- an in-memory stand-in for the database ----------
class FakeConn:
    def __init__(self, coins=100_000, rank=0, rarity="common", owner=1, listed=False):
        self.cat = {"owner_id": owner, "clone_id": None, "rarity": rarity, "cat_rank": rank}
        self.coins, self.listed = coins, listed
        self.stats = None
        self.skills = {}

    @asynccontextmanager
    async def transaction(self):
        snap = (self.coins, dict(self.stats) if self.stats else None, dict(self.skills))
        try:
            yield
        except BaseException:
            self.coins, self.stats, self.skills = snap[0], snap[1], snap[2]
            raise

    async def fetchrow(self, sql, *a):
        if "FROM cat_adoptions" in sql:
            return self.cat
        if "FROM cat_stats" in sql:
            return dict(self.stats) if self.stats else None

    async def fetch(self, sql, *a):
        return [{"skill_key": k} for _, k in sorted((s, k) for s, k in self.skills.items())]

    async def fetchval(self, sql, *a):
        if "cat_listings" in sql:
            return 1 if self.listed else None
        if "UPDATE cat_wallets" in sql:
            if self.coins >= a[2]:
                self.coins -= a[2]
                return self.coins
            return None
        if "SELECT coins" in sql:
            return self.coins

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO cat_stats"):
            self.stats = self.stats or {"strength": 0, "guard": 0, "speed": 0, "spirit": 0, "respec_count": 0}
        elif "SET strength = $2" in sql:
            self.stats.update(dict(zip(cs.STATS, a[1:5])))
        elif "SET strength = 0" in sql:
            self.stats.update({s: 0 for s in cs.STATS}, respec_count=self.stats["respec_count"] + 1)
        elif sql.startswith("DELETE FROM cat_skills"):
            self.skills = {}
        elif sql.startswith("INSERT INTO cat_skills"):
            self.skills[a[1]] = a[2]


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = FakeConn(**kw)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        async def get_pool():
            return Pool()
        monkeypatch.setattr(cs, "_pool", get_pool)
        return conn
    return make


def test_apply_points_saves_and_only_adds(db):
    conn = db()
    a = {"strength": 4, "guard": 2, "speed": 0, "spirit": 0}
    r = run(cs.apply_points(7, 1, None, 0, a))
    assert r.status == "ok" and r.stats.alloc() == a
    assert run(cs.apply_points(7, 1, None, 0, {**a, "strength": 3})).status == "shrink"
    assert conn.stats["strength"] == 4
    r = run(cs.apply_points(7, 1, None, 0, {**a, "speed": 2}))
    assert r.status == "ok" and r.stats.spent() == 8
    assert run(cs.apply_points(7, 1, None, 0, {**a, "speed": 3})).status == "pool"


def test_apply_points_guards(db):
    db(owner=2)
    assert run(cs.apply_points(7, 1, None, 0, {s: 0 for s in cs.STATS})).status == "not_yours"
    db(rank=3)
    assert run(cs.apply_points(7, 1, None, 0, {s: 0 for s in cs.STATS})).status == "stale"       # stale screen
    db(listed=True)
    assert run(cs.apply_points(7, 1, None, 0, {s: 0 for s in cs.STATS})).status == "listed"


def test_ranking_up_gives_more_points_with_no_write(db):
    conn = db()
    run(cs.apply_points(7, 1, None, 0, {"strength": 4, "guard": 4, "speed": 0, "spirit": 0}))
    assert run(cs.apply_points(7, 1, None, 0, {"strength": 4, "guard": 4, "speed": 1, "spirit": 0})).status == "pool"
    conn.cat["cat_rank"] = 1                                  # rank up: pool 10
    r = run(cs.apply_points(7, 1, None, 1, {"strength": 5, "guard": 4, "speed": 1, "spirit": 0}))
    assert r.status == "ok"


def test_first_respec_is_free_then_it_costs_and_a_double_tap_cannot_charge_twice(db):
    conn = db(coins=50_000, rank=5)
    run(cs.apply_points(7, 1, None, 5, {"strength": 6, "guard": 6, "speed": 0, "spirit": 0}))
    r = run(cs.respec(7, 1, None, 5, 0))
    assert r.status == "ok" and r.cost == 0 and conn.coins == 50_000 and r.stats.spent() == 0 and r.stats.respec_count == 1
    assert run(cs.respec(7, 1, None, 5, 0)).status == "stale"                # same screen tapped twice
    run(cs.apply_points(7, 1, None, 5, {"strength": 6, "guard": 0, "speed": 0, "spirit": 0}))
    r = run(cs.respec(7, 1, None, 5, 1))
    assert r.status == "ok" and r.cost == 20_000 and conn.coins == 30_000 and r.stats.spent() == 0


def test_respec_without_coins_changes_nothing(db):
    conn = db(coins=100, rank=5)
    run(cs.apply_points(7, 1, None, 5, {"strength": 6, "guard": 0, "speed": 0, "spirit": 0}))
    conn.stats["respec_count"] = 1
    r = run(cs.respec(7, 1, None, 5, 1))
    assert r.status == "poor" and r.cost == 20_000 and r.coins == 100
    assert conn.coins == 100 and conn.stats["strength"] == 6 and conn.stats["respec_count"] == 1


def test_set_skills_replaces_and_validates(db):
    conn = db(rank=5)
    assert run(cs.set_skills(7, 1, None, 5, ["pounce", "hiss"])).stats.skills == ("pounce", "hiss")
    assert run(cs.set_skills(7, 1, None, 5, ["hiss"])).stats.skills == ("hiss",)
    assert run(cs.set_skills(7, 1, None, 5, ["yarn_trap"])).status == "rank"
    assert run(cs.set_skills(7, 1, None, 5, ["a", "b", "c"])).status == "slots"
    assert conn.skills == {1: "hiss"}                                        # a refused change leaves the old skills


# ---------- screen budget (40 components, 4000 characters) ----------
def _count(view):
    return sum(1 for _ in view.walk_children())


class Parent:
    async def interaction_check(self, i):
        return True


@pytest.mark.parametrize("rank,rarity,spent", [(0, "common", 0), (12, "rare", 20), (25, "legendary", 66)])
def test_screen_fits_the_discord_budget(rank, rarity, spent):
    c = cat(rank, rarity)
    st = cs.Stats(strength=min(spent, cs.stat_cap(cs.pool(rarity, rank))), skills=tuple(list(cs.SKILLS)[:cs.slots_for(rank)]))
    view = vs.StatsView(Parent(), c, st, note="x" * 200)
    assert _count(view) <= 40 and len(view.text()) < 4000
    view.armed = True
    view._build()
    assert _count(view) <= 40 and len(view.text()) < 4000


def test_plus_buttons_stop_at_the_cap_and_the_pool():
    view = vs.StatsView(Parent(), cat(0), cs.Stats())
    for _ in range(10):
        if view._can_add("strength"):
            view.pending["strength"] += 1
    assert view.pending["strength"] == 4 and not view._can_add("strength") and view._can_add("guard")


# ---------- wiring ----------
def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/057_cat_stats_skills.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_stats" in sql and "CREATE TABLE IF NOT EXISTS cat_skills" in sql
    assert not re.search(r"\bDROP\b|(?<!ON )\bDELETE\b|\bTRUNCATE\b", sql, re.I)
    db = (ROOT / "database.py").read_text()
    assert "057_cat_stats_skills.sql" in db and 'SCHEMA_VERSION = "' in db


def test_my_cats_has_the_stats_button_and_shows_stats():
    src = (ROOT / "discord_bot/cogs/_views_cat_adopt.py").read_text()
    assert "Stats & skills" in src and "cat_stats.get_stats" in src and "_stats_line" in src
