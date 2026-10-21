"""Phase 8 Homes: tiers and prices, buy / move up in one transaction, vault deposit and withdraw, screen budget, hub wiring, help."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import discord
from PIL import Image

from discord_bot.cogs import _views_cat_home as vh
from discord_bot.cogs import _views_cat_hub as hub
from modules import cat_help, cat_hub
from modules import cat_home as ch
from modules import cat_wallet as cw

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


# ── a tiny fake database: wallets {owner: [coins, primole]}, homes {(owner, key): row}; transactions roll back on error ──

class Tx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self):
        self.snap = copy.deepcopy((self.conn.wallets, self.conn.homes)); return self
    async def __aexit__(self, et, ev, tb):
        if et is not None:
            self.conn.wallets, self.conn.homes = self.snap
        return False


class Conn:
    def __init__(self, wallets, homes=None):
        self.wallets, self.homes = wallets, homes or {}

    def transaction(self): return Tx(self)

    async def fetchrow(self, sql, *a):
        owner = a[0]
        w = self.wallets.get(owner)
        if sql.startswith("UPDATE cat_wallets SET coins = coins - "):
            n = a[1]
            if not w or w[0] < n: return None
            w[0] -= n; return {"coins": w[0]}
        if sql.startswith("UPDATE cat_wallets SET primole = primole - "):
            n = a[2]
            if not w or w[1] < n: return None
            w[1] -= n; return {"primole": w[1]}
        h = self.homes.get((owner, a[1] if len(a) > 1 else -1))
        if sql.startswith("SELECT tier FROM cat_homes"):
            return {"tier": h["tier"]} if h else None
        if sql.startswith("SELECT tier, vault FROM cat_homes"):
            return {"tier": h["tier"], "vault": h["vault"]} if h else None
        if sql.startswith("SELECT vault FROM cat_homes"):
            return {"vault": h["vault"]} if h else None
        if sql.startswith("SELECT tier, vault, invested_coins"):
            return dict(h) if h else None
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        h = self.homes[(a[0], a[1])]
        if "vault = vault + " in sql:
            h["vault"] += a[2]; return h["vault"]
        if "vault = vault - " in sql:
            if h["vault"] < a[2]: return None
            h["vault"] -= a[2]; return h["vault"]
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO cat_homes"):
            owner, key, tier, coins, prim = a
            h = self.homes.get((owner, key))
            if h:
                h.update(tier=tier, invested_coins=h["invested_coins"] + coins, invested_primole=h["invested_primole"] + prim)
            else:
                self.homes[(owner, key)] = dict(tier=tier, vault=0, invested_coins=coins, invested_primole=prim)
            return
        if sql.startswith("INSERT INTO cat_wallets"):
            self.wallets.setdefault(a[0], [0, 0])[0] += a[1]; return
        raise AssertionError(sql)


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return self
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def use(monkeypatch, wallets, homes=None):
    conn = Conn(wallets, homes)

    async def pool():
        return Pool(conn)
    monkeypatch.setattr(ch, "_pool", pool)
    return conn


# ── tiers and pure rules ──

def test_five_tiers_each_dearer_than_the_last_with_a_picture():
    assert [t.key for t in ch.TIERS] == ["beach", "cottage", "penthouse", "rocket", "castle"]
    for a, b in zip(ch.TIERS, ch.TIERS[1:]):
        assert b.coins > a.coins and b.primole >= a.primole and b.vault > a.vault      # moving up never costs less of anything
    assert [t.primole for t in ch.TIERS[:2]] == [0, 0] and all(t.primole > 0 for t in ch.TIERS[2:])
    for t in ch.TIERS:
        assert t.vault < t.coins and t.name and t.emoji and t.blurb
        with Image.open(ROOT / "assets/images/cat_homes" / f"{t.key}.png") as im:
            assert im.size[0] >= 400 and im.size[1] >= 400, t.key
        assert ch.image_url(t.key).endswith(f"/{t.key}.png")
    assert len(list((ROOT / "assets/images/cat_homes").glob("*.png"))) == len(ch.TIERS)


def test_cost_to_reach():
    assert ch.cost_to_reach(None, "castle") == (400_000_000, 1_500)
    assert ch.cost_to_reach(None, "beach") == (150_000, 0)
    assert ch.cost_to_reach("cottage", "rocket") == (59_000_000, 300)          # only the difference
    assert ch.cost_to_reach("penthouse", "castle") == (392_000_000, 1_450)
    assert ch.cost_to_reach("rocket", "rocket") is None and ch.cost_to_reach("rocket", "beach") is None
    assert ch.cost_to_reach(None, "nope") is None and ch.cost_to_reach("beach", "nope") is None
    # climbing step by step costs exactly the same as buying the top one outright
    steps = [ch.cost_to_reach(a.key, b.key) for a, b in zip(ch.TIERS, ch.TIERS[1:])]
    assert sum(c for c, _ in steps) + ch.TIERS[0].coins == ch.TIERS[-1].coins
    assert sum(p for _, p in steps) + ch.TIERS[0].primole == ch.TIERS[-1].primole


def test_parse_amount():
    ok = {"10000": 10_000, "12,500": 12_500, "10k": 10_000, "2.5m": 2_500_000, " 1 B ": 1_000_000_000, "7": 7}
    for text, n in ok.items():
        assert ch.parse_amount(text) == n, text
    for bad in ("", "0", "-5", "abc", "1.2.3", "10x", "k", "1e5", "99999999999999999"):
        assert ch.parse_amount(bad) is None, bad


def test_vault_room_and_bar():
    t = ch.tier("beach")
    assert ch.vault_room(t, 0) == 50_000 and ch.vault_room(t, 20_000) == 30_000 and ch.vault_room(t, 99_999_999) == 0
    assert ch.vault_bar(0, 100) == "▱" * 10 and ch.vault_bar(100, 100) == "▰" * 10 and ch.vault_bar(50, 100).count("▰") == 5
    assert ch.vault_bar(5, 0) == "▱" * 10


# ── buying ──

def test_buy_pays_coins_and_primole_and_creates_the_home(monkeypatch):
    conn = use(monkeypatch, {1: [10_000_000, 60]})
    res = run(ch.buy(1, None, "penthouse"))
    assert (res.status, res.coins, res.primole) == ("ok", 8_000_000, 50)
    assert conn.wallets[1] == [2_000_000, 10]
    assert conn.homes[(1, -1)] == dict(tier="penthouse", vault=0, invested_coins=8_000_000, invested_primole=50)
    assert run(ch.get_home(1, None)).tier == "penthouse"


def test_buy_without_enough_coins_takes_nothing(monkeypatch):
    conn = use(monkeypatch, {1: [7_999_999, 500]})
    res = run(ch.buy(1, None, "penthouse"))
    assert res.status == "poor"
    assert conn.wallets[1] == [7_999_999, 500] and not conn.homes            # the Primole that was taken first came back


def test_buy_without_enough_primole_takes_nothing(monkeypatch):
    conn = use(monkeypatch, {1: [9_000_000, 49]})
    assert run(ch.buy(1, None, "penthouse")).status == "poor_primole"
    assert conn.wallets[1] == [9_000_000, 49] and not conn.homes
    assert run(ch.buy(99, None, "beach")).status == "poor" and not conn.homes   # no wallet at all


def test_one_home_and_moving_up_pays_the_difference(monkeypatch):
    conn = use(monkeypatch, {1: [100_000_000, 400]})
    assert run(ch.buy(1, None, "cottage")).status == "ok"
    assert run(ch.buy(1, None, "cottage")).status == "same"
    assert run(ch.buy(1, None, "beach")).status == "lower"
    assert run(ch.buy(1, None, "nope")).status == "unknown"
    assert conn.wallets[1] == [99_000_000, 400]                              # the repeats cost nothing
    res = run(ch.buy(1, None, "rocket"))
    assert (res.status, res.coins, res.primole) == ("ok", 59_000_000, 300)
    assert conn.wallets[1] == [40_000_000, 100]
    h = conn.homes[(1, -1)]
    assert h["tier"] == "rocket" and h["invested_coins"] == 60_000_000 and h["invested_primole"] == 300     # = the rocket's price
    assert len([k for k in conn.homes if k[0] == 1]) == 1


def test_homes_are_per_bot(monkeypatch):
    conn = use(monkeypatch, {1: [10_000_000, 0]})
    assert run(ch.buy(1, None, "beach")).status == "ok"
    assert run(ch.buy(1, 5, "beach")).status == "ok"                         # a clone has its own home
    assert set(conn.homes) == {(1, -1), (1, 5)}


def test_primole_only_ever_goes_down(monkeypatch):
    conn = use(monkeypatch, {1: [10 ** 10, 2_000]})
    seen = []
    for key in ("penthouse", "penthouse", "rocket", "castle", "beach"):
        run(ch.buy(1, None, key)); seen.append(conn.wallets[1][1])
    assert seen == sorted(seen, reverse=True) and seen[-1] == 2_000 - 1_500
    run(ch.deposit(1, None, None)); run(ch.withdraw(1, None, None))
    assert conn.wallets[1][1] == seen[-1]


# ── the vault ──

def test_deposit_and_withdraw_keep_every_coin(monkeypatch):
    conn = use(monkeypatch, {1: [1_000_000, 0]}, {(1, -1): dict(tier="beach", vault=0, invested_coins=0, invested_primole=0)})
    total = lambda: conn.wallets[1][0] + conn.homes[(1, -1)]["vault"]
    res = run(ch.deposit(1, None, 30_000))
    assert (res.status, res.moved, res.vault, res.coins) == ("ok", 30_000, 30_000, 970_000) and total() == 1_000_000
    res = run(ch.deposit(1, None, None))                                       # max: only what still fits (50,000 cap)
    assert (res.status, res.moved, res.vault) == ("ok", 20_000, 50_000) and total() == 1_000_000
    assert run(ch.deposit(1, None, 1)).status == "full" and run(ch.deposit(1, None, None)).status == "full"
    res = run(ch.withdraw(1, None, 15_000))
    assert (res.status, res.moved, res.vault) == ("ok", 15_000, 35_000) and total() == 1_000_000
    assert run(ch.withdraw(1, None, 35_001)).status == "empty" and total() == 1_000_000
    res = run(ch.withdraw(1, None, None))
    assert (res.status, res.moved, res.vault) == ("ok", 35_000, 0) and conn.wallets[1][0] == 1_000_000
    assert run(ch.withdraw(1, None, None)).status == "empty"


def test_deposit_guards(monkeypatch):
    conn = use(monkeypatch, {1: [10_000, 0]}, {(1, -1): dict(tier="beach", vault=0, invested_coins=0, invested_primole=0)})
    assert run(ch.deposit(1, None, 20_000)).status == "poor"                   # more than the wallet holds: nothing moves
    assert conn.wallets[1][0] == 10_000 and conn.homes[(1, -1)]["vault"] == 0
    assert run(ch.deposit(1, None, 60_000)).status == "full"                   # more than the vault can hold
    assert run(ch.deposit(1, None, 0)).status == "bad" and run(ch.deposit(1, None, -4)).status == "bad"
    assert run(ch.withdraw(1, None, 0)).status == "bad"
    assert run(ch.deposit(2, None, 5)).status == "no_home" and run(ch.withdraw(2, None, 5)).status == "no_home"
    assert conn.wallets[1][0] == 10_000


# ── screens ──

def _count(view):
    n = 0
    for item in view.walk_children():
        n += 1
    return n


def _fake_interaction(edits):
    async def edit_message(**kw):
        edits.append(kw)
    return SimpleNamespace(user=SimpleNamespace(id=1), client=SimpleNamespace(clone_id=None), message=None, data={},
                           response=SimpleNamespace(edit_message=edit_message, is_done=lambda: False))


def _view(monkeypatch, home, wallet, shown=None):
    async def get_home(owner, clone): return home

    async def safe(owner, clone): return wallet
    monkeypatch.setattr(ch, "get_home", get_home)
    monkeypatch.setattr(cw, "safe_wallet", safe)
    v = vh.HomeView()
    edits = []
    run(v.reload(_fake_interaction(edits)))
    if shown:
        v.shown = shown; v._build()
    return v, edits


def test_screen_budget_for_every_state(monkeypatch):
    rich = cw.Wallet(10 ** 12, 99, 99_999)
    for home in (None, ch.Home("beach", 0), ch.Home("rocket", 25_000_000, 60_000_000, 300), ch.Home("castle", 150_000_000, 400_000_000, 1_500)):
        for t in ch.TIERS:
            v, _ = _view(monkeypatch, home, rich, t.key)
            for armed in (False, True):
                v.armed = armed; v.note = "x" * 200; v._build()
                assert _count(v) <= 40 and len(v.text()) < 4000, (home, t.key)
    v, _ = _view(monkeypatch, None, rich)
    assert any(isinstance(c, discord.ui.MediaGallery) for c in v.walk_children())


def test_screen_text_tells_the_truth():
    none = ch.screen_text(None, cw.Wallet(5, 0, 0), ch.tier("penthouse"))
    assert "don't own a home" in none and "8,000,000" in none and "50" in none and "Vault size" in none
    own = ch.screen_text(ch.Home("cottage", 1_000), cw.Wallet(), ch.tier("cottage"))
    assert "This is your home" in own and "1,000" in own and "400,000" in own
    up = ch.screen_text(ch.Home("cottage", 0), cw.Wallet(), ch.tier("rocket"), armed=True)
    assert "Move up" in up and "59,000,000" in up and "300" in up and "Confirm" in up
    low = ch.screen_text(ch.Home("rocket", 0), cw.Wallet(), ch.tier("beach"))
    assert "bigger home" in low and "Price" not in low and "Move up" not in low


def test_buying_takes_two_taps(monkeypatch):
    v, edits = _view(monkeypatch, None, cw.Wallet(10 ** 9, 0, 0), "cottage")
    calls = []

    async def fake_buy(owner, clone, key):
        calls.append(key)
        return ch.BuyResult("ok", key, 1_000_000, 0)
    monkeypatch.setattr(ch, "buy", fake_buy)
    run(v._buy(_fake_interaction(edits)))
    assert calls == [] and v.armed                                             # first tap only arms the confirm
    run(v._buy(_fake_interaction(edits)))
    assert calls == ["cottage"] and not v.armed
    assert "You now own" in vh.buy_note(ch.BuyResult("ok", "cottage", 1_000_000, 0))


def test_changing_the_home_on_show_disarms_the_confirm(monkeypatch):
    v, edits = _view(monkeypatch, None, cw.Wallet(10 ** 9, 0, 0), "cottage")
    v.armed = True
    it = _fake_interaction(edits); it.data = {"values": ["castle"]}
    run(v._pick(it))
    assert v.shown == "castle" and not v.armed


def test_vault_notes_cover_every_status():
    for st in ("ok", "no_home", "bad", "full", "poor", "empty"):
        for dep in (True, False):
            assert vh.vault_note(ch.VaultResult(st, 5, 5), dep)
    for st in ("ok", "poor", "poor_primole", "same", "lower", "unknown"):
        assert vh.buy_note(ch.BuyResult(st, "beach", 1, 0))


# ── wiring ──

def test_home_is_in_the_cat_menu_and_opens_the_home_screen():
    keys = [k for k, *_ in cat_hub.MENU]
    assert "home" in keys and len(keys) <= 25
    assert isinstance(hub._button_for("home"), vh.HomeButton)
    assert vh.HomeButton().custom_id == "cat_feature:home"


def test_help_mentions_homes_and_stays_short():
    assert "Home" in [t[2] for t in cat_help.topics()] and "vault" in cat_help.topic_text("home")
    assert any("Home" in title for title, _ in cat_help.sections())
    assert len(cat_help.build_text()) < 4000


def test_no_new_slash_commands_and_rent_is_untouched():
    src = (ROOT / "discord_bot/cogs/_views_cat_home.py").read_text() + (ROOT / "modules/cat_home.py").read_text()
    assert "app_commands" not in src and "@bot.tree" not in src
    assert "cat_life" not in src                                               # the rent system is not touched by homes
    assert "primole = primole +" not in src


def test_migration_is_additive_and_registered():
    sql = (ROOT / "database/migrations/077_cat_homes.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_homes" in sql and "DROP" not in sql.upper() and "ALTER TABLE" not in sql.upper()
    assert "077_cat_homes.sql" in (ROOT / "database.py").read_text()
