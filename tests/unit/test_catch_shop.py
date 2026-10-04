"""Shop and Wallet: price rules, SQL contracts, screens, timing, double-click guard."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_shop as views
import discord_bot.cogs.catch as catch
from modules import catch_items
from modules import catch_shop as shop
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, fetchval=None, rows=()):
        self._fetchval, self.rows = list(fetchval or []), list(rows)
        self.executed, self.queries = [], []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self._fetchval.pop(0) if self._fetchval else None

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(shop.catch_db, "connection", fake)
    monkeypatch.setattr(shop.catch_db, "transaction", fake)


# ---- pure rules ---------------------------------------------------------------------------------

def test_catalog_only_sells_known_items_at_positive_prices_and_never_the_sovereign_capsule():
    assert shop.CATALOG and all(key in catch_items.KNOWN_ITEMS for key in shop.CATALOG)
    assert all(isinstance(price, int) and price > 0 for price in shop.CATALOG.values())
    assert "capsule_sovereign" not in shop.CATALOG


def test_total_price_uses_the_catalog_and_rejects_bad_input():
    assert shop.total_price("capsule_sturdy", 5) == shop.CATALOG["capsule_sturdy"] * 5
    for item, qty in (("capsule_sovereign", 1), ("nope", 1), ("capsule_basic", 3), ("capsule_basic", 0), ("capsule_basic", -1)):
        try:
            shop.total_price(item, qty)
        except (KeyError, ValueError):
            continue
        raise AssertionError(f"{item} x{qty} should be rejected")


def test_wallet_entries_signed_and_zero_or_unknown_rows_dropped():
    daily = shop.entry_from_row("daily", {"streak": 3, "coins": 150}, NOW)
    assert daily.coins == 150 and "day 3" in daily.summary
    bought = shop.entry_from_row("shop_purchase", json.dumps({"item": "goldberry", "quantity": 2, "total": 240}), NOW)
    assert bought.coins == -240 and "Goldberry ×2" in bought.summary
    assert shop.entry_from_row("daily", {"streak": 1, "coins": 0}, NOW) is None
    assert shop.entry_from_row("starter_kit", {}, NOW) is None
    assert shop.entry_from_row("shop_purchase", "not json", NOW) is None


# ---- SQL contracts ------------------------------------------------------------------------------

def test_purchase_deducts_with_a_guard_grants_items_and_audits_in_one_transaction(monkeypatch):
    db = FakeDb(fetchval=[880])
    patch_db(monkeypatch, db)
    result = asyncio.run(shop.purchase(7, None, "capsule_sturdy", 5, guild_id=42))
    total = shop.CATALOG["capsule_sturdy"] * 5
    assert (result.ok, result.total, result.coins_left, result.quantity) == (True, total, 880, 5)
    sql, args = db.queries[0]
    assert "coins >= $3" in sql and "RETURNING coins" in sql and args == (7, -1, total)
    assert "catch_inventory" in db.executed[0][0] and db.executed[0][1] == (7, None, "capsule_sturdy", 5)
    audit_sql, audit_args = db.executed[1]
    assert "'shop_purchase'" in audit_sql and audit_args[:3] == (7, None, 42)
    detail = json.loads(audit_args[3])
    assert detail == {"item": "capsule_sturdy", "quantity": 5, "unit_price": shop.CATALOG["capsule_sturdy"], "total": total}


def test_purchase_scopes_by_clone_key(monkeypatch):
    db = FakeDb(fetchval=[10])
    patch_db(monkeypatch, db)
    asyncio.run(shop.purchase(7, 3, "honeyberry", 1))
    assert db.queries[0][1][1] == 3


def test_purchase_refused_when_short_on_coins_changes_nothing(monkeypatch):
    db = FakeDb(fetchval=[None, 20])
    patch_db(monkeypatch, db)
    result = asyncio.run(shop.purchase(7, None, "capsule_prime", 10))
    assert (result.ok, result.reason, result.coins_left) == (False, "insufficient_coins", 20)
    assert result.total == shop.CATALOG["capsule_prime"] * 10
    assert db.executed == []  # no grant, no audit


def test_purchase_rejects_unsold_items_and_odd_quantities_before_touching_the_db(monkeypatch):
    db = FakeDb()
    patch_db(monkeypatch, db)
    assert asyncio.run(shop.purchase(7, None, "capsule_sovereign", 1)).reason == "not_for_sale"
    assert asyncio.run(shop.purchase(7, None, "capsule_basic", 99)).reason == "bad_quantity"
    assert asyncio.run(shop.purchase(7, None, "capsule_basic", -1)).reason == "bad_quantity"
    assert db.queries == [] and db.executed == []


def test_load_wallet_reads_balance_and_recent_coin_movements(monkeypatch):
    rows = [
        {"action": "shop_purchase", "detail": '{"item": "honeyberry", "quantity": 1, "total": 40}', "at": NOW},
        {"action": "daily", "detail": {"streak": 1, "coins": 100}, "at": NOW},
    ]
    db = FakeDb(fetchval=[60], rows=rows)
    patch_db(monkeypatch, db)
    wallet = asyncio.run(shop.load_wallet(7, None))
    assert wallet.coins == 60 and [e.coins for e in wallet.entries] == [-40, 100]
    history_sql, history_args = db.queries[1]
    assert "ORDER BY id DESC LIMIT" in history_sql and history_args[0] == 7 and history_args[1] == -1
    assert set(history_args[2]) == set(shop.WALLET_ACTIONS)


def test_load_wallet_for_a_new_player_is_empty(monkeypatch):
    patch_db(monkeypatch, FakeDb(fetchval=[None], rows=[]))
    wallet = asyncio.run(shop.load_wallet(7, None))
    assert wallet.coins == 0 and wallet.entries == ()


# ---- screens ------------------------------------------------------------------------------------

def buy_buttons(view):
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


def test_shop_view_fits_discord_limits_and_gates_buttons_on_selection_and_balance():
    view = views.ShopView(7, None, coins=0)
    rows = {c.row for c in view.children}
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert len(view.children) <= 25 and len(rows) <= 5 and len(selects[0].options) == len(shop.CATALOG) <= 25
    assert all(b.disabled for b in buy_buttons(view))  # nothing selected

    view = views.ShopView(7, None, coins=shop.CATALOG["capsule_basic"] * 5, selected="capsule_basic")
    enabled = {b.label: not b.disabled for b in buy_buttons(view)}
    assert enabled[f"Buy ×1 · {shop.CATALOG['capsule_basic']:,} coins"] is True
    assert enabled[f"Buy ×5 · {shop.CATALOG['capsule_basic'] * 5:,} coins"] is True
    assert enabled[f"Buy ×10 · {shop.CATALOG['capsule_basic'] * 10:,} coins"] is False


def test_shop_embed_fits_embed_limits_and_marks_selection():
    embed = views.shop_embed(1234, "goldberry", notice="hello")
    assert len(embed) <= 6000 and "▶ **Goldberry**" in embed.description and embed.fields[0].value == "1,234"


def test_wallet_embed_shows_signed_amounts_and_empty_state():
    wallet = shop.Wallet(60, (shop.WalletEntry("daily", 100, "Daily reward (day 1)", NOW), shop.WalletEntry("shop_purchase", -40, "Honeyberry ×1", NOW)))
    embed = views.wallet_embed(wallet)
    body = embed.fields[1].value
    assert "`+100`" in body and "`−40`" in body and embed.fields[0].value == "60"
    assert views.wallet_embed(shop.Wallet(0, ())).description == catch.text("wallet.empty")


def shop_interaction(rec, values=None):
    inter = make_interaction(rec)
    inter.data = {"values": values} if values is not None else {}
    return inter


def gate(rec, allowed=True):
    return slow(rec, "gate", SimpleNamespace(allowed=allowed, reason=None if allowed else "shop_disabled"))


def test_open_shop_and_wallet_respond_before_gate_and_db(monkeypatch):
    for opener, loader_name, loader_result in (
        (views.open_shop, "load_player", catch_items.PlayerSummary(coins=5)),
        (views.open_wallet, "load_wallet", shop.Wallet(5, ())),
    ):
        rec = Recorder()
        monkeypatch.setattr(views, "check_player_allowed", gate(rec))
        monkeypatch.setattr(views, loader_name, slow(rec, "load", loader_result))
        asyncio.run(opener(shop_interaction(rec)))
        assert_response_first(rec)
        assert rec.calls.index("gate") < rec.calls.index("load")


def test_open_shop_refuses_when_the_shop_feature_is_off(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec, allowed=False))
    monkeypatch.setattr(views, "load_player", slow(rec, "load", catch_items.PlayerSummary()))
    inter = shop_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0] if args else kwargs)

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.open_shop(inter))
    assert sent == [catch.text("catch.unavailable", reason="shop_disabled")] and "load" not in rec.calls


def test_choosing_an_item_responds_first_then_redraws(monkeypatch):
    rec = Recorder()
    view = views.ShopView(7, None, coins=1000)
    asyncio.run(view._choose(shop_interaction(rec, ["goldberry"])))
    assert_response_first(rec)
    assert view.selected == "goldberry" and "edit_original" in rec.calls
    asyncio.run(view._choose(shop_interaction(Recorder(), ["capsule_sovereign"])))  # not in catalog
    assert view.selected is None


def test_buy_responds_first_charges_the_catalog_price_and_redraws(monkeypatch):
    rec, seen = Recorder(), {}
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def fake_purchase(user_id, clone_id, item, quantity, **kwargs):
        rec.add("purchase")
        await asyncio.sleep(0.05)
        seen.update(user=user_id, item=item, quantity=quantity, guild=kwargs.get("guild_id"))
        return shop.PurchaseResult(True, None, item, quantity, 300, 700)

    monkeypatch.setattr(views, "purchase", fake_purchase)
    view = views.ShopView(7, None, coins=1000, selected="capsule_sturdy")
    asyncio.run(view._buy(shop_interaction(rec), 1))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("purchase") < rec.calls.index("edit_original")
    assert seen == {"user": 7, "item": "capsule_sturdy", "quantity": 1, "guild": 1}
    assert view.coins == 700 and "Sturdy capsule ×1" in view.notice


def test_buy_is_blocked_when_the_shop_was_turned_off_while_the_view_was_open(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate(rec, allowed=False))
    monkeypatch.setattr(views, "purchase", slow(rec, "purchase"))
    view = views.ShopView(7, None, coins=1000, selected="capsule_basic")
    asyncio.run(view._buy(shop_interaction(rec), 1))
    assert "purchase" not in rec.calls


def test_double_click_buys_once(monkeypatch):
    rec, calls = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def fake_purchase(user_id, clone_id, item, quantity, **kwargs):
        calls.append(quantity)
        await asyncio.sleep(0.05)
        return shop.PurchaseResult(True, None, item, quantity, 50, 950)

    monkeypatch.setattr(views, "purchase", fake_purchase)
    view = views.ShopView(7, None, coins=1000, selected="capsule_basic")

    async def both():
        await asyncio.gather(view._buy(shop_interaction(rec), 1), view._buy(shop_interaction(rec), 1))

    asyncio.run(both())
    assert calls == [1]
    asyncio.run(view._buy(shop_interaction(rec), 1))  # a later, separate click works again
    assert calls == [1, 1]


def test_buy_failure_reports_no_charge_and_does_not_redraw(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "purchase", boom)
    inter = shop_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    inter.followup = SimpleNamespace(send=followup_send)
    view = views.ShopView(7, None, coins=1000, selected="capsule_basic")
    asyncio.run(view._buy(inter, 1))
    assert sent == [catch.text("shop.error")] and "edit_original" not in rec.calls and view.coins == 1000
    assert view._busy is False  # a failure must not lock the buttons


def test_buy_insufficient_coins_updates_balance_and_explains(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))
    monkeypatch.setattr(views, "purchase", slow(rec, "purchase", shop.PurchaseResult(False, "insufficient_coins", "capsule_prime", 1, 500, 20)))
    view = views.ShopView(7, None, coins=1000, selected="capsule_prime")  # stale balance on screen
    asyncio.run(view._buy(shop_interaction(rec), 1))
    assert view.coins == 20 and "500" in view.notice and "20" in view.notice


def test_buy_without_a_selection_asks_for_one(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))
    monkeypatch.setattr(views, "purchase", slow(rec, "purchase"))
    inter = shop_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.ShopView(7, None, coins=1000)._buy(inter, 1))
    assert sent == [catch.text("shop.no_selection")] and "purchase" not in rec.calls


def test_only_the_owner_can_use_the_shop():
    sent = []

    async def send_message(*args, **kwargs):
        sent.append(args[0])

    view = views.ShopView(7, None, coins=10)
    other = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=send_message))
    mine = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(view.interaction_check(other)) is False and sent == [catch.text("ui.not_yours")]
    assert asyncio.run(view.interaction_check(mine)) is True


# ---- hub wiring ---------------------------------------------------------------------------------

def test_economy_buttons_open_the_real_shop_and_wallet_screens():
    assert catch.REAL_ACTIONS["shop"] is views.open_shop and catch.REAL_ACTIONS["wallet"] is views.open_wallet
    economy = {b.label: b.callback for b in catch.CatchHubView(category="economy").children if isinstance(b, discord.ui.Button)}
    assert economy["Shop"] is views.open_shop and economy["Wallet"] is views.open_wallet
    assert economy["Sell"] not in (views.open_shop, views.open_wallet)


def test_dynamic_button_routes_shop_and_wallet_after_restart(monkeypatch):
    seen = []

    async def mark(name):
        seen.append(name)

    for key in ("shop", "wallet"):
        monkeypatch.setitem(catch.REAL_ACTIONS, key, lambda interaction, key=key: mark(key))
        asyncio.run(catch.CatchHubDynamicButton(discord.ui.Button(custom_id=f"catch:hub:{key}"), action=key).callback(SimpleNamespace()))
    assert seen == ["shop", "wallet"]
