"""Cat wallet (Phase 2): coins and diamonds come with every adoption; 1 diamond = 10,000 coins; play currency only."""
import asyncio
import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_adopt as ca
from modules import cat_pool
from modules import cat_wallet as cw
from tests.unit.test_cat_adopt import FI, mkcat, _msg_with_embed  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def no_images(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value="https://cdn.discordapp.com/a/b/c.jpg"))


# ── rules ─────────────────────────────────────────────────────────────────

class Rng:
    def __init__(self, roll, base=100):
        self.roll, self.base = roll, base

    def random(self): return self.roll
    def randint(self, a, b): return self.base


def test_constants_match_the_design():
    assert cw.DIAMOND_COINS == 10_000 and cw.DIAMOND_CHANCE_PERCENT == 3 and (cw.COIN_MIN, cw.COIN_MAX) == (50, 500)


def test_low_roll_is_a_diamond_and_it_is_one_diamond_whatever_the_rarity():
    for rarity in cw.RARITY_MULTIPLIER:
        assert cw.roll_reward(rarity, Rng(0.0299)) == cw.Reward("diamond", 1)


def test_roll_at_or_above_the_chance_is_coins_scaled_by_rarity():
    assert cw.roll_reward("common", Rng(0.03, 100)) == cw.Reward("coins", 100)
    assert cw.roll_reward("uncommon", Rng(0.5, 100)) == cw.Reward("coins", 150)
    assert cw.roll_reward("rare", Rng(0.5, 100)) == cw.Reward("coins", 250)
    assert cw.roll_reward("legendary", Rng(0.99, 100)) == cw.Reward("coins", 500)
    assert cw.roll_reward("mystery", Rng(0.5, 100)) == cw.Reward("coins", 100)        # unknown rarity = x1


def test_coin_rewards_stay_inside_the_range_for_every_rarity():
    rng = random.Random(3)
    for rarity, mult in cw.RARITY_MULTIPLIER.items():
        amounts = [r.amount for r in (cw.roll_reward(rarity, rng) for _ in range(4000)) if r.kind == "coins"]
        assert min(amounts) >= round(cw.COIN_MIN * mult) and max(amounts) <= round(cw.COIN_MAX * mult)


def test_diamond_share_is_about_three_percent():
    rng = random.Random(11)
    n = 30000
    d = sum(cw.roll_reward("common", rng).kind == "diamond" for _ in range(n))
    assert 0.025 < d / n < 0.035


def test_texts():
    assert "**1,250 coins**" in cw.reward_text(cw.Reward("coins", 1250))
    t = cw.reward_text(cw.Reward("diamond", 1))
    assert "diamond" in t and "10,000" in t and "My cats" in t
    assert cw.reward_text(None) == ""
    assert cw.wallet_text(cw.Wallet(1234567, 1)) == "🪙 1,234,567 coins · 💎 1 diamond"
    assert cw.wallet_text(cw.Wallet(0, 2)) == "🪙 0 coins · 💎 2 diamonds"


# ── an in-memory database that behaves like the SQL ───────────────────────

class Tx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): self.conn.depth += 1; self.conn.max_depth = max(self.conn.max_depth, self.conn.depth)
    async def __aexit__(self, *a): self.conn.depth -= 1; return False


class Conn:
    def __init__(self, owned=0, spawn_open=True, wallet=None, fail_wallet=False):
        self.owned, self.spawn_open, self.fail_wallet = owned, spawn_open, fail_wallet
        self.wallet = dict(wallet or {})          # (owner, clone key) -> [coins, diamonds]
        self.depth = self.max_depth = 0
        self.wallet_writes = []

    def transaction(self): return Tx(self)

    async def fetchval(self, sql, *a):
        if "COUNT(*)" in sql:
            return self.owned
        if "FROM cat_adoptions WHERE pool_id" in sql:      # one cat, one owner across all bots: nobody owns it yet
            return None
        return 1

    async def fetchrow(self, sql, *a):
        if sql.startswith("UPDATE cat_spawns"):
            return {"pool_id": 100} if self.spawn_open else None
        if sql.startswith("SELECT adopted_by FROM cat_spawns"):
            return {"adopted_by": 7}                                   # somebody else already has it
        if sql.startswith("INSERT INTO cat_adoptions"):
            return {"id": 1, "owner_id": a[0], "clone_id": a[1], "pool_id": a[2], "name": a[3], "rarity": a[4], "trait": a[5],
                    "adopted_at": None, "traded_count": 0}
        if "SELECT coins, diamonds FROM cat_wallets" in sql:
            w = self.wallet.get((a[0], a[1]))
            return {"coins": w[0], "diamonds": w[1]} if w else None
        if sql.startswith("UPDATE cat_wallets"):
            owner, key, third = a[0], a[1], a[2]
            w = self.wallet.get((owner, key))
            if "diamonds = 0" in sql:                                  # convert all
                if not w or w[1] < 1:
                    return None
                w[0] += w[1] * third; w[1] = 0
            else:                                                      # convert N
                n = third
                if not w or w[1] < n:
                    return None
                w[1] -= n; w[0] += n * a[3]
            return {"coins": w[0], "diamonds": w[1]}
        return None

    async def execute(self, sql, *a):
        if "cat_wallets" in sql:
            if self.fail_wallet:
                raise RuntimeError("wallet table missing")
            self.wallet_writes.append((a, self.depth))
            w = self.wallet.setdefault((a[0], -1 if a[1] is None else a[1]), [0, 0])
            w[0] += a[2]; w[1] += a[3]


def use_db(monkeypatch, conn):
    class Acq:
        async def __aenter__(self): return conn
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def pool():
        return Pool()
    monkeypatch.setattr(ca, "_pool", pool)
    monkeypatch.setattr(cw, "_pool", pool)
    return conn


# ── adoption grants the reward ────────────────────────────────────────────

def test_adopting_gives_the_reward_inside_the_same_transaction_as_the_cat(monkeypatch):
    conn = use_db(monkeypatch, Conn())
    monkeypatch.setattr(cw, "roll_reward", lambda rarity, rng=random: cw.Reward("coins", 321))
    res = run(ca.adopt(1, 10, None, 5))
    assert res.status == "ok" and res.reward == cw.Reward("coins", 321)
    assert conn.wallet == {(10, -1): [321, 0]}
    assert conn.wallet_writes[0][1] == 2          # the reward ran in a savepoint nested in the adoption transaction


def test_a_diamond_lands_as_a_diamond(monkeypatch):
    conn = use_db(monkeypatch, Conn())
    monkeypatch.setattr(cw, "roll_reward", lambda rarity, rng=random: cw.Reward("diamond", 1))
    run(ca.adopt(1, 10, 7, 5))
    assert conn.wallet == {(10, -1): [0, 1]}          # a clone's reward lands in the one shared wallet


def test_rewards_add_up_across_adoptions(monkeypatch):
    conn = use_db(monkeypatch, Conn())
    monkeypatch.setattr(cw, "roll_reward", lambda rarity, rng=random: cw.Reward("coins", 100))
    run(ca.adopt(1, 10, None, 5)); run(ca.adopt(2, 10, None, 5))
    assert conn.wallet[(10, -1)] == [200, 0]


def test_a_wallet_failure_never_undoes_the_adoption(monkeypatch):
    use_db(monkeypatch, Conn(fail_wallet=True))
    res = run(ca.adopt(1, 10, None, 5))
    assert res.status == "ok" and res.cat is not None and res.reward is None


def test_no_reward_when_the_adoption_itself_fails(monkeypatch):
    for kwargs, status in ((dict(spawn_open=False), "taken"), (dict(owned=ca.MAX_CATS), "limit")):
        conn = use_db(monkeypatch, Conn(**kwargs))
        res = run(ca.adopt(1, 10, None, 5))
        assert res.status == status and res.reward is None and conn.wallet == {}


def test_reward_follows_the_cats_rarity(monkeypatch):
    use_db(monkeypatch, Conn())
    seen = []
    monkeypatch.setattr(ca, "roll_rarity", lambda rng=random: "legendary")
    monkeypatch.setattr(cw, "roll_reward", lambda rarity, rng=random: seen.append(rarity) or cw.Reward("coins", 1))
    run(ca.adopt(1, 10, None, 5))
    assert seen == ["legendary"]


# ── converting ────────────────────────────────────────────────────────────

def test_convert_one_diamond_is_10000_coins(monkeypatch):
    conn = use_db(monkeypatch, Conn(wallet={(10, -1): [250, 3]}))
    assert run(cw.convert(10, None, 1)) == cw.Wallet(10_250, 2)
    assert conn.wallet[(10, -1)] == [10_250, 2]


def test_convert_all(monkeypatch):
    use_db(monkeypatch, Conn(wallet={(10, -1): [0, 3]}))
    assert run(cw.convert(10, None, None)) == cw.Wallet(30_000, 0)


def test_cannot_convert_what_you_dont_have(monkeypatch):
    conn = use_db(monkeypatch, Conn(wallet={(10, -1): [500, 0]}))
    assert run(cw.convert(10, None, 1)) is None and run(cw.convert(10, None, None)) is None
    assert run(cw.convert(99, None, 1)) is None                       # a person with no wallet at all
    assert conn.wallet[(10, -1)] == [500, 0]


def test_one_wallet_for_every_bot(monkeypatch):
    use_db(monkeypatch, Conn(wallet={(10, -1): [5, 1]}))
    assert run(cw.get_wallet(10, None)) == cw.Wallet(5, 1) and run(cw.get_wallet(10, 7)) == cw.Wallet(5, 1)
    assert run(cw.get_wallet(11, 7)) == cw.Wallet()
    assert run(cw.convert(10, 7, 1)) == cw.Wallet(5 + cw.DIAMOND_COINS, 0)     # spending from a clone uses the same wallet


def test_migration_merges_per_bot_wallets_without_deleting():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "database/migrations/049_cat_shared_wallet.sql").read_text()
    assert "SUM(coins)" in sql and "GROUP BY owner_id" in sql and "DELETE FROM" not in sql.upper() and "DROP " not in sql.upper()
    assert "clone_id IS NOT NULL" in sql


def test_safe_wallet_never_raises(monkeypatch):
    async def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(cw, "get_wallet", boom)
    assert run(cw.safe_wallet(10, None)) == cw.Wallet()


# ── Adopt button shows it ─────────────────────────────────────────────────

def _adopt_with(monkeypatch, reward):
    cat = mkcat(1, owner=10)
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("ok", cat, reward)))
    i = FI(uid=10, message=_msg_with_embed())
    run(v.AdoptButton(5).callback(i))
    return i.followups[0][0]


def test_adopt_message_says_what_came_with_the_cat(monkeypatch):
    t = _adopt_with(monkeypatch, cw.Reward("coins", 420))
    assert "420 coins" in t and "Open **My cats**" in t and "adopted" in t
    t = _adopt_with(monkeypatch, cw.Reward("diamond", 1))
    assert "diamond" in t and "10,000" in t


def test_adopt_message_is_unchanged_when_there_is_no_reward(monkeypatch):
    t = _adopt_with(monkeypatch, None)
    assert "coins" not in t and "diamond" not in t and "Open **My cats**" in t


# ── My cats screen ────────────────────────────────────────────────────────

def _view(monkeypatch, wallet, convert=None):
    async def safe(owner, clone):
        return wallet
    monkeypatch.setattr(cw, "safe_wallet", safe)
    if convert is not None:
        monkeypatch.setattr(cw, "convert", convert)
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1), mkcat(2)])
    run(view.refresh_wallet()); view._build()
    return view


def _labels(view):
    return [(c.label, c.disabled) for c in view.children if isinstance(c, discord.ui.Button)]


def test_purse_is_shown_on_the_cat_card(monkeypatch):
    view = _view(monkeypatch, cw.Wallet(1234, 2))
    e = run(view.embed())
    assert [f.name for f in e.fields] == ["Your purse"] and e.fields[0].value == "🪙 1,234 coins · 💎 2 diamonds"


def test_convert_buttons_are_disabled_without_diamonds_and_on_with_them(monkeypatch):
    none = dict(_labels(_view(monkeypatch, cw.Wallet(500, 0))))
    assert none["💎 Convert 1"] is True and none["💎 Convert all (0)"] is True
    some = dict(_labels(_view(monkeypatch, cw.Wallet(500, 4))))
    assert some["💎 Convert 1"] is False and some["💎 Convert all (4)"] is False
    assert all(c.row == 1 for c in _view(monkeypatch, cw.Wallet(0, 1)).children if isinstance(c, discord.ui.Button) and "Convert" in c.label)


def test_convert_one_converts_one_refreshes_and_says_so(monkeypatch):
    calls = []

    async def convert(owner, clone, count):
        calls.append((owner, clone, count)); return cw.Wallet(10_500, 1)
    view = _view(monkeypatch, cw.Wallet(500, 2), convert)
    state = {"w": cw.Wallet(10_500, 1)}

    async def safe(owner, clone):
        return state["w"]
    monkeypatch.setattr(cw, "safe_wallet", safe)
    i = FI(uid=10)
    run(view._convert_one(i))
    assert calls == [(10, None, 1)]
    edit = i.edits[0]
    assert "Converted" in edit["content"] and "10,500" in edit["content"]
    assert edit["embed"].fields[0].value == "🪙 10,500 coins · 💎 1 diamond" and dict(_labels(view))["💎 Convert all (1)"] is False


def test_convert_all_passes_none(monkeypatch):
    calls = []

    async def convert(owner, clone, count):
        calls.append(count); return cw.Wallet(30_000, 0)
    view = _view(monkeypatch, cw.Wallet(0, 3), convert)
    run(view._convert_all(FI(uid=10)))
    assert calls == [None]


def test_convert_with_nothing_to_convert_is_a_polite_message(monkeypatch):
    async def convert(owner, clone, count):
        return None
    view = _view(monkeypatch, cw.Wallet(0, 1), convert)
    i = FI(uid=10)
    run(view._convert_one(i))
    assert "don't have a diamond" in i.edits[0]["content"]


def test_convert_error_is_reported_not_raised(monkeypatch):
    async def convert(owner, clone, count):
        raise RuntimeError("db down")
    view = _view(monkeypatch, cw.Wallet(0, 1), convert)
    i = FI(uid=10)
    run(view._convert_one(i))
    assert i.sent and i.sent[0][1]["ephemeral"] is True and not i.edits


def test_only_the_owner_can_press_convert(monkeypatch):
    view = _view(monkeypatch, cw.Wallet(0, 1))
    other = FI(uid=99)
    assert run(view.interaction_check(other)) is False and other.sent


def test_opening_my_cats_loads_the_wallet(monkeypatch):
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1)]))

    async def safe(owner, clone):
        return cw.Wallet(77, 1)
    monkeypatch.setattr(cw, "safe_wallet", safe)
    i = FI(uid=10)
    run(v.open_collection(i))
    assert i.sent[0][1]["embed"].fields[0].value == "🪙 77 coins · 💎 1 diamond"


# ── play money only ───────────────────────────────────────────────────────

def test_coins_never_move_between_people():
    trade_src = Path(ROOT / "modules" / "cat_adopt.py").read_text()
    start = trade_src.index("async def trade(")
    assert "cat_wallets" not in trade_src[start:]
    wallet_src = (ROOT / "modules" / "cat_wallet.py").read_text()
    assert not re.search(r"def (transfer|send|gift|pay|buy|sell)", wallet_src)


def test_migration_is_additive_idempotent_and_registered():
    sql = (ROOT / "database" / "migrations" / "035_cat_wallet.sql").read_text()
    code = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    body = "\n".join(code)
    assert "CREATE TABLE IF NOT EXISTS cat_wallets" in body and "CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_wallets" in body
    assert "CHECK (coins >= 0)" in body and "CHECK (diamonds >= 0)" in body
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE)\b", body, re.I)
    db = (ROOT / "database.py").read_text()
    assert "035_cat_wallet.sql" in db and int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 80
