"""Sell: value rules, SQL contracts, screens, timing, double-click guard, hub wiring."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import discord_bot.cogs._views_catch_sell as views
import discord_bot.cogs.catch as catch
from modules import catch_game, catch_items
from modules import catch_sell as sell
from modules import catch_shop as shop
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def owned(species_id=1, **over):
    base = {"id": 11, "species_id": species_id, "level": 9, "shiny": False, "special": False, "favorite": False, "locked": False}
    base.update(over)
    return base


class FakeDb:
    def __init__(self, *, fetchrow=None, fetchval=None, rows=()):
        self._row, self._fetchval, self.rows = fetchrow, list(fetchval or []), list(rows)
        self.queries, self.executed = [], []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self._row

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

    monkeypatch.setattr(sell.catch_db, "connection", fake)
    monkeypatch.setattr(sell.catch_db, "transaction", fake)


def species_of(rarity):
    from modules.catch_species import all_species
    return next(sid for sid, sp in all_species().items() if sp["rarity"] == rarity)


# ---- value rules --------------------------------------------------------------------------------

def test_sale_value_uses_rarity_table_and_multipliers_that_do_not_stack():
    for rarity in catch_game.RARITIES:
        assert sell.sale_value(rarity.key) == rarity.sell_value > 0
    assert sell.sale_value("rare", shiny=True) == catch_game.RARITY_BY_KEY["rare"].sell_value * catch_game.SHINY_SELL_MULTIPLIER
    assert sell.sale_value("rare", special=True) == catch_game.RARITY_BY_KEY["rare"].sell_value * catch_game.SPECIAL_SELL_MULTIPLIER
    assert sell.sale_value("rare", shiny=True, special=True) == sell.sale_value("rare", special=True)
    assert [sell.sale_value(r.key) for r in catch_game.RARITIES] == sorted(sell.sale_value(r.key) for r in catch_game.RARITIES)


# ---- SQL contracts ------------------------------------------------------------------------------

def test_sell_locks_validates_ownership_deletes_pays_and_audits_in_one_transaction(monkeypatch):
    sid = species_of("rare")
    db = FakeDb(fetchrow=owned(sid, shiny=True), fetchval=[11, 1500])
    patch_db(monkeypatch, db)
    result = asyncio.run(sell.sell_creature(11, 7, None, guild_id=42))
    value = sell.sale_value("rare", shiny=True)
    assert (result.ok, result.value, result.coins_left) == (True, value, 1500)
    select_sql, select_args = db.queries[0]
    assert "FOR UPDATE" in select_sql and "user_id = $2" in select_sql and "clone_key = $3" in select_sql
    assert select_args == (11, 7, -1)
    delete_sql, delete_args = db.queries[1]
    assert delete_sql.startswith("DELETE FROM catch_owned") and "RETURNING id" in delete_sql and delete_args == (11, 7, -1)
    credit_sql, credit_args = db.queries[2]
    assert "coins = coins + $3" in credit_sql and "RETURNING coins" in credit_sql and credit_args == (7, -1, value)
    assert len(db.executed) == 1
    audit_sql, audit_args = db.executed[0]
    assert "'sell'" in audit_sql and audit_args[:3] == (7, None, 42)
    detail = json.loads(audit_args[3])
    assert detail["owned_id"] == 11 and detail["coins"] == value and detail["shiny"] is True and detail["rarity"] == "rare"


def test_sell_scopes_by_clone_key(monkeypatch):
    db = FakeDb(fetchrow=owned(species_of("common")), fetchval=[11, 10])
    patch_db(monkeypatch, db)
    asyncio.run(sell.sell_creature(11, 7, 3))
    assert db.queries[0][1][2] == 3 and db.queries[1][1][2] == 3 and db.queries[2][1][1] == 3


def test_sell_refuses_missing_foreign_favourite_locked_and_unpriceable_creatures_without_changes(monkeypatch):
    cases = (
        (None, "not_found"),
        (owned(species_of("common"), favorite=True), "favorite"),
        (owned(species_of("common"), locked=True), "locked"),
        (owned(99999), "unknown_species"),
    )
    for row, reason in cases:
        db = FakeDb(fetchrow=row)
        patch_db(monkeypatch, db)
        result = asyncio.run(sell.sell_creature(11, 7, None))
        assert (result.ok, result.reason) == (False, reason)
        assert db.executed == [] and len(db.queries) == 1  # nothing deleted, paid or audited


def test_nothing_is_paid_when_the_delete_removed_no_row(monkeypatch):
    db = FakeDb(fetchrow=owned(species_of("common")), fetchval=[None])  # lost the race: DELETE returned nothing
    patch_db(monkeypatch, db)
    result = asyncio.run(sell.sell_creature(11, 7, None))
    assert (result.ok, result.reason) == (False, "not_found")
    assert len(db.queries) == 2 and db.executed == []  # no credit, no audit


def test_sale_rolls_back_when_the_player_row_is_missing(monkeypatch):
    db = FakeDb(fetchrow=owned(species_of("common")), fetchval=[11, None])
    patch_db(monkeypatch, db)
    with pytest.raises(RuntimeError):
        asyncio.run(sell.sell_creature(11, 7, None))
    assert not any("'sell'" in sql for sql, _ in db.executed)  # raised before the audit row


def test_list_sellable_hides_favourites_and_locked_and_prices_rows(monkeypatch):
    sid = species_of("epic")
    rec = {"id": 5, "species_id": sid, "level": 30, "nickname": None, "shiny": False, "special": True}
    db = FakeDb(fetchval=[1], rows=[rec])
    patch_db(monkeypatch, db)
    rows, total, page = asyncio.run(sell.list_sellable(7, None, page=4))
    assert (total, page) == (1, 0) and rows[0].value == sell.sale_value("epic", special=True)
    for sql, _ in db.queries:
        assert "NOT favorite" in sql and "NOT locked" in sql and "clone_key = $2" in sql


def test_wallet_shows_sales_as_positive_lines():
    entry = shop.entry_from_row("sell", {"name": "Emberpup", "coins": 120}, NOW)
    assert entry.coins == 120 and "Sold Emberpup" in entry.summary
    assert shop.entry_from_row("sell", {"name": "x", "coins": 0}, NOW) is None
    assert "sell" in shop.WALLET_ACTIONS


# ---- screens ------------------------------------------------------------------------------------

def make_rows(n, rarity="common"):
    sid = species_of(rarity)
    return [sell.SellRow(i + 1, sid, "Emberpup", rarity, 5, None, False, False, sell.sale_value(rarity)) for i in range(n)]


def test_sell_view_fits_discord_limits_and_gates_buttons_on_selection():
    rows = make_rows(sell.SELL_PAGE_SIZE)
    view = views.SellView(7, None, 100, rows, total=25)
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(view.children) <= 25 and len({c.row for c in view.children}) <= 5
    assert len(selects[0].options) == sell.SELL_PAGE_SIZE <= 25
    by_label = {b.label: b for b in buttons}
    assert by_label[catch.text("sell.button_idle")].disabled and by_label[catch.text("sell.cancel")].disabled
    assert by_label["◀"].disabled and not by_label["▶"].disabled
    view.selected = rows[0]
    view._build()
    labels = {b.label: b.disabled for b in view.children if isinstance(b, discord.ui.Button)}
    assert labels[catch.text("sell.button", value=rows[0].value)] is False


def test_sell_view_with_nothing_to_sell_has_no_select_and_says_why():
    view = views.SellView(7, None, 5, [], total=0)
    assert not [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert catch.text("sell.empty") in view.embed().description


def test_sell_embed_fits_embed_limits_and_marks_selection():
    rows = make_rows(sell.SELL_PAGE_SIZE, "mythic")
    embed = views.sell_embed(1234, rows, 0, 10, sell.SELL_PAGE_SIZE, rows[1], notice="hello")
    assert len(embed) <= 6000 and "▶ " in embed.description and embed.fields[0].value == "1,234"


def sell_interaction(rec, values=None):
    inter = make_interaction(rec)
    inter.data = {"values": values} if values is not None else {}
    return inter


def gate(rec, allowed=True):
    return slow(rec, "gate", SimpleNamespace(allowed=allowed, reason=None if allowed else "shop_disabled"))


def test_open_sell_responds_before_gate_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))
    monkeypatch.setattr(views, "load_player", slow(rec, "load_player", catch_items.PlayerSummary(coins=5)))
    monkeypatch.setattr(views, "list_sellable", slow(rec, "list", (make_rows(2), 2, 0)))
    asyncio.run(views.open_sell(sell_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("load_player")


def test_open_sell_refuses_when_the_shop_feature_is_off(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec, allowed=False))
    monkeypatch.setattr(views, "list_sellable", slow(rec, "list", ([], 0, 0)))
    inter = sell_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.open_sell(inter))
    assert sent == [catch.text("catch.unavailable", reason="shop_disabled")] and "list" not in rec.calls


def test_choose_responds_first_selects_only_listed_creatures(monkeypatch):
    rec = Recorder()
    rows = make_rows(3)
    view = views.SellView(7, None, 0, rows, total=3)
    asyncio.run(view._choose(sell_interaction(rec, ["2"])))
    assert_response_first(rec)
    assert view.selected.id == 2 and "edit_original" in rec.calls
    asyncio.run(view._choose(sell_interaction(Recorder(), ["999"])))  # forged id not on screen
    assert view.selected is None


def test_cancel_clears_selection(monkeypatch):
    rec = Recorder()
    view = views.SellView(7, None, 0, make_rows(2), total=2)
    view.selected = view.rows[0]
    asyncio.run(view._cancel(sell_interaction(rec)))
    assert_response_first(rec)
    assert view.selected is None


def test_sell_responds_first_pays_the_service_value_and_redraws(monkeypatch):
    rec, seen = Recorder(), {}
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def fake_sell(owned_id, user_id, clone_id, **kwargs):
        rec.add("sell")
        await asyncio.sleep(0.05)
        seen.update(owned=owned_id, user=user_id, guild=kwargs.get("guild_id"))
        return sell.SellResult(True, None, "Emberpup", 10, 110)

    monkeypatch.setattr(views, "sell_creature", fake_sell)
    monkeypatch.setattr(views, "list_sellable", slow(rec, "reload", (make_rows(1)[:0], 0, 0)))
    view = views.SellView(7, None, 100, make_rows(2), total=2)
    view.selected = view.rows[1]
    asyncio.run(view._sell(sell_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("sell") < rec.calls.index("reload") < rec.calls.index("edit_original")
    assert seen == {"owned": 2, "user": 7, "guild": 1}
    assert view.coins == 110 and "Sold" in view.notice and view.selected is None


def test_sell_is_blocked_when_shop_was_turned_off_while_open(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate(rec, allowed=False))
    monkeypatch.setattr(views, "sell_creature", slow(rec, "sell"))
    view = views.SellView(7, None, 0, make_rows(1), total=1)
    view.selected = view.rows[0]
    asyncio.run(view._sell(sell_interaction(rec)))
    assert "sell" not in rec.calls


def test_double_click_sells_once(monkeypatch):
    rec, calls = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def fake_sell(owned_id, user_id, clone_id, **kwargs):
        calls.append(owned_id)
        await asyncio.sleep(0.05)
        return sell.SellResult(True, None, "Emberpup", 10, 110)

    monkeypatch.setattr(views, "sell_creature", fake_sell)
    monkeypatch.setattr(views, "list_sellable", slow(rec, "reload", ([], 0, 0)))
    view = views.SellView(7, None, 100, make_rows(1), total=1)
    view.selected = view.rows[0]

    async def both():
        await asyncio.gather(view._sell(sell_interaction(rec)), view._sell(sell_interaction(rec)))

    asyncio.run(both())
    assert calls == [1] and view._busy is False


def test_sell_failure_reports_nothing_sold_and_unlocks_buttons(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "sell_creature", boom)
    inter = sell_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    inter.followup = SimpleNamespace(send=followup_send)
    view = views.SellView(7, None, 100, make_rows(1), total=1)
    view.selected = view.rows[0]
    asyncio.run(view._sell(inter))
    assert sent == [catch.text("sell.error")] and "edit_original" not in rec.calls
    assert view.coins == 100 and view._busy is False


@pytest.mark.parametrize("reason,key", [("favorite", "sell.favorite"), ("locked", "sell.locked"),
                                        ("not_found", "sell.gone"), ("unknown_species", "sell.unknown")])
def test_refusals_explain_and_leave_coins_alone(monkeypatch, reason, key):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))
    monkeypatch.setattr(views, "sell_creature", slow(rec, "sell", sell.SellResult(False, reason)))
    monkeypatch.setattr(views, "list_sellable", slow(rec, "reload", (make_rows(1), 1, 0)))
    view = views.SellView(7, None, 100, make_rows(1), total=1)
    view.selected = view.rows[0]
    asyncio.run(view._sell(sell_interaction(rec)))
    assert view.coins == 100 and view.notice.startswith(catch.text(key, name="Emberpup")[:12])


def test_sell_without_selection_asks_for_one(monkeypatch):
    rec, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", gate(rec))
    monkeypatch.setattr(views, "sell_creature", slow(rec, "sell"))
    inter = sell_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(args[0])

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.SellView(7, None, 0, make_rows(1), total=1)._sell(inter))
    assert sent == [catch.text("sell.no_selection")] and "sell" not in rec.calls


def test_paging_responds_first_and_reloads(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "list_sellable", slow(rec, "reload", (make_rows(3), 13, 1)))
    view = views.SellView(7, None, 0, make_rows(10), total=13)
    asyncio.run(view._next(sell_interaction(rec)))
    assert_response_first(rec)
    assert view.page == 1 and view.total == 13


def test_only_the_owner_can_use_sell():
    sent = []

    async def send_message(*args, **kwargs):
        sent.append(args[0])

    view = views.SellView(7, None, 0, make_rows(1), total=1)
    other = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=send_message))
    mine = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(view.interaction_check(other)) is False and sent == [catch.text("ui.not_yours")]
    assert asyncio.run(view.interaction_check(mine)) is True


# ---- hub wiring ---------------------------------------------------------------------------------

def test_economy_sell_button_opens_the_real_sell_screen():
    assert catch.REAL_ACTIONS["sell"] is views.open_sell
    economy = {b.label: b.callback for b in catch.CatchHubView(category="economy").children if isinstance(b, discord.ui.Button)}
    assert economy["Sell"] is views.open_sell
