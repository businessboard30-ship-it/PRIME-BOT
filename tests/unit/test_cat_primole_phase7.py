"""Phase 7 Primole: wallet column, one-way diamond -> Primole conversion, spend helper, account-bound rules, purse button."""
import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_wallet as cw
from tests.unit.test_cat_adopt import FI, mkcat

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Row(dict):
    pass


class Conn:
    """Tiny fake wallet: dispatches on the SQL text. state = {owner: [coins, diamonds, primole]}."""
    def __init__(self, state):
        self.state = state
        self.sql = []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        owner = a[0]
        w = self.state.get(owner)
        if sql.startswith("UPDATE") and "primole = primole + " in sql:
            n, rate = a[2], a[3]
            if not w or w[1] < n:
                return None
            w[1] -= n; w[2] += n * rate
            return Row(coins=w[0], diamonds=w[1], primole=w[2])
        if sql.startswith("UPDATE") and "primole = primole - " in sql:
            n = a[2]
            if not w or w[2] < n:
                return None
            w[2] -= n
            return Row(primole=w[2])
        if sql.startswith("SELECT"):
            return Row(coins=w[0], diamonds=w[1], primole=w[2]) if w else None
        raise AssertionError(sql)


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return self
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def use(monkeypatch, state):
    conn = Conn(state)

    async def pool():
        return Pool(conn)
    monkeypatch.setattr(cw, "_pool", pool)
    return conn


# rules

def test_constants():
    assert cw.DIAMOND_PRIMOLE == 100 and cw.PRIMOLE == "🔮" and cw.DIAMOND_COINS == 10_000


def test_wallet_text_hides_primole_until_you_have_some():
    assert cw.wallet_text(cw.Wallet(5, 1)) == "🪙 5 coins · 💎 1 diamond"
    assert cw.wallet_text(cw.Wallet(5, 1, 250)) == "🪙 5 coins · 💎 1 diamond · 🔮 250 Primole"


def test_migration_is_additive_and_loaded_and_version_bumped():
    sql = (ROOT / "database/migrations/068_primole.sql").read_text()
    body = re.sub(r"(?m)^\s*--.*$", "", sql).strip().upper()
    assert body.startswith("ALTER TABLE CAT_WALLETS ADD COLUMN IF NOT EXISTS PRIMOLE BIGINT NOT NULL DEFAULT 0")
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE)\b", body)
    db = (ROOT / "database.py").read_text()
    assert "068_primole.sql" in db and int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 117


# conversion

def test_convert_one_diamond_gives_100_primole_and_keeps_coins(monkeypatch):
    conn = use(monkeypatch, {10: [250, 3, 0]})
    assert run(cw.convert_to_primole(10, None, 1)) == cw.Wallet(250, 2, 100)
    assert conn.state[10] == [250, 2, 100]


def test_convert_several_and_clone_uses_the_shared_wallet(monkeypatch):
    use(monkeypatch, {10: [0, 3, 50]})
    assert run(cw.convert_to_primole(10, 7, 3)) == cw.Wallet(0, 0, 350)


def test_cannot_convert_without_diamonds_or_with_bad_counts(monkeypatch):
    conn = use(monkeypatch, {10: [500, 0, 0]})
    assert run(cw.convert_to_primole(10, None, 1)) is None
    assert run(cw.convert_to_primole(99, None, 1)) is None
    assert run(cw.convert_to_primole(10, None, 0)) is None
    assert run(cw.convert_to_primole(10, None, -5)) is None
    assert conn.state[10] == [500, 0, 0]


def test_conversion_is_one_atomic_guarded_statement():
    src = inspect.getsource(cw.convert_to_primole)
    assert src.count("conn.fetchrow") == 1 and "diamonds >= $3" in src and "UPDATE cat_wallets" in src


def test_double_spend_of_one_diamond_only_works_once(monkeypatch):
    conn = use(monkeypatch, {10: [0, 1, 0]})
    first = run(cw.convert_to_primole(10, None, 1))
    second = run(cw.convert_to_primole(10, None, 1))
    assert first == cw.Wallet(0, 0, 100) and second is None and conn.state[10] == [0, 0, 100]


def test_no_way_back_exists():
    names = [n for n in dir(cw) if callable(getattr(cw, n))]
    for n in names:
        if "primole" in n.lower():
            assert n in ("convert_to_primole", "spend_primole"), n
    src = inspect.getsource(cw)
    assert "diamonds = diamonds + " not in src and "coins = coins + primole" not in src
    assert not re.search(r"primole[^\n]*\bcoins\s*=\s*coins\s*\+", src)


# spending

def test_spend_primole(monkeypatch):
    conn = Conn({10: [0, 0, 120]})
    assert run(cw.spend_primole(conn, 10, 100)) == 20
    assert run(cw.spend_primole(conn, 10, 100)) is None          # can't afford: nothing changes
    assert conn.state[10] == [0, 0, 20]
    assert run(cw.spend_primole(conn, 10, 0)) is None and run(cw.spend_primole(conn, 10, -3)) is None
    assert run(cw.spend_primole(conn, 11, 1)) is None


def test_primole_is_account_bound_nothing_else_touches_it():
    for f in (ROOT / "modules").glob("*.py"):
        if f.name == "cat_wallet.py":
            continue
        txt = f.read_text()
        # cat_home.py is the one place Primole is spent (homes cost it); it only ever calls cw.spend_primole.
        assert "primole" not in txt.lower() or f.name in ("cat_primole.py", "cat_home.py"), f.name
    home = (ROOT / "modules/cat_home.py").read_text()
    assert "primole = primole +" not in home and "primole + " not in home.replace("invested_primole + ", "")
    assert "primole" not in (ROOT / "modules/cat_market.py").read_text().lower()


# purse button

def _view(monkeypatch, wallet, conv=None):
    async def safe(owner, clone):
        return wallet
    monkeypatch.setattr(cw, "safe_wallet", safe)
    if conv is not None:
        monkeypatch.setattr(cw, "convert_to_primole", conv)
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1), mkcat(2)])
    run(view.refresh_wallet()); view._build()
    return view


def _buttons(view):
    return [(c.label, c.disabled) for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def test_button_disabled_without_diamond_and_enabled_with(monkeypatch):
    off = dict(_buttons(_view(monkeypatch, cw.Wallet(0, 0))))
    on = dict(_buttons(_view(monkeypatch, cw.Wallet(0, 2))))
    assert off["🔮 1 → 100 Primole"] is True and on["🔮 1 → 100 Primole"] is False


def test_first_tap_only_arms_second_tap_converts(monkeypatch):
    calls = []

    async def conv(owner, clone, count):
        calls.append((owner, clone, count)); return cw.Wallet(0, 1, 100)
    view = _view(monkeypatch, cw.Wallet(0, 2), conv)
    i = FI(uid=10)
    run(view._convert_primole(i))
    assert calls == [] and any("final" in l for l, _ in _buttons(view))
    state = {"w": cw.Wallet(0, 1, 100)}

    async def safe(owner, clone):
        return state["w"]
    monkeypatch.setattr(cw, "safe_wallet", safe)
    i2 = FI(uid=10)
    run(view._convert_primole(i2))
    assert calls == [(10, None, 1)]
    card = v.view_text(i2.edits[0]["view"])
    assert "100 Primole" in card and "can't be undone" in card
    assert not any("final" in l for l, _ in _buttons(view))        # disarmed again


def test_other_money_actions_disarm_the_primole_button(monkeypatch):
    view = _view(monkeypatch, cw.Wallet(0, 2))
    view.confirm_primole = True
    async def conv(owner, clone, count):
        return cw.Wallet(10_000, 1)
    monkeypatch.setattr(cw, "convert", conv)
    run(view._convert_one(FI(uid=10)))
    assert view.confirm_primole is False


def test_screen_budget_with_the_new_button(monkeypatch):
    view = _view(monkeypatch, cw.Wallet(1234, 2, 500))
    assert len(list(view.walk_children())) <= 40
    assert len(v.view_text(view)) < 4000
