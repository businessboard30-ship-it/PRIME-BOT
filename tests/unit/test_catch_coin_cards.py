"""Coin banner and daily reward card: rendering, fallbacks, and how each coin screen uses them."""

import asyncio
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_items as items_views
import discord_bot.cogs._views_catch_sell as sell_views
import discord_bot.cogs._views_catch_shop as shop_views
from modules import catch_coin_card as cards
from modules.catch_game import BALLS
from modules.catch_i18n import text
from modules.catch_items import DailyResult, DailyReward
from modules.catch_shop import CATALOG, Wallet
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow
from tests.unit.test_catch_phase3 import edits_to

PNG = b"\x89PNG\r\n\x1a\n"
BALL = next(iter(BALLS))


def run(coro):
    return asyncio.run(coro)


def size(data: bytes):
    return Image.open(io.BytesIO(data)).size


def sends_to(inter):
    sent = []

    async def send(*args, **kwargs):
        sent.append({"args": args, **kwargs})

    inter.followup = SimpleNamespace(send=send)
    return sent


def gate_ok(monkeypatch, module, rec):
    monkeypatch.setattr(module, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))


# ---------------------------------------------------------------- rendering

@pytest.mark.parametrize("coins", [0, 7, 1234, 250_000_000, 10**15, -5])
def test_banner_renders_for_any_balance(coins):
    data = cards._cached_banner("SHOP", coins)
    assert data.startswith(PNG) and size(data) == (cards.W, cards.BANNER_H)


def test_banner_changes_with_balance_and_label():
    base = cards._cached_banner("SHOP", 100)
    assert cards._cached_banner("SHOP", 101) != base and cards._cached_banner("SELL", 100) != base
    assert cards._cached_banner("SHOP", -5) == cards._cached_banner("SHOP", 0)  # never draws a negative balance


@pytest.mark.parametrize("streak", [0, 1, 7, 8, 365])
def test_daily_card_renders_for_any_streak(streak):
    data = cards._cached_daily(250, streak, ((BALL, 3),))
    assert data.startswith(PNG) and size(data) == (cards.W, cards.DAILY_H)


def test_daily_card_shows_streak_pips_items_and_survives_many_items():
    base = cards._cached_daily(250, 3, (("A", 1),))
    assert cards._cached_daily(250, 4, (("A", 1),)) != base      # one more pip
    assert cards._cached_daily(250, 3, (("A", 2),)) != base      # quantity drawn
    assert cards._cached_daily(250, 3, ()) != base               # no items
    crowded = tuple((f"Very Long Item Name {i}", 99) for i in range(9))
    assert size(cards._cached_daily(250, 3, crowded)) == (cards.W, cards.DAILY_H)  # extra pills are skipped


def test_streak_label_follows_the_streak():
    assert cards._cached_daily(250, 8, (("A", 1),)) != cards._cached_daily(250, 7, (("A", 1),))


# ---------------------------------------------------------------- helpers

def test_coin_art_attaches_the_banner_and_drops_only_the_balance_field():
    embed = discord.Embed(title="t")
    embed.add_field(name="Your coins", value="9")
    embed.add_field(name="Other", value="x")
    file = run(cards.coin_art(embed, label="SHOP", coins=9, drop_field="Your coins"))
    assert file.filename == cards.COIN_FILE and embed.image.url == f"attachment://{cards.COIN_FILE}"
    assert [f.name for f in embed.fields] == ["Other"]


def test_coin_art_failure_leaves_the_embed_untouched(monkeypatch):
    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_banner", boom)
    embed = discord.Embed(title="t")
    embed.add_field(name="Your coins", value="9")
    assert run(cards.coin_art(embed, label="SHOP", coins=9, drop_field="Your coins")) is None
    assert embed.image.url is None and len(embed.fields) == 1


def test_daily_art_failure_leaves_the_embed_untouched(monkeypatch):
    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_daily", boom)
    embed = discord.Embed(title="t")
    embed.add_field(name="Coins", value="+1")
    assert run(cards.daily_art(embed, coins=1, streak=1, items={"x": 1}, drop_fields=("Coins",))) is None
    assert embed.image.url is None and len(embed.fields) == 1


def test_kwargs_helpers_clear_a_stale_card_on_edit_and_omit_file_on_send():
    file = discord.File(io.BytesIO(b"x"), filename="a.png")
    assert cards.edit_kwargs(file) == {"attachments": [file]} and cards.edit_kwargs(None) == {"attachments": []}
    assert cards.send_kwargs(file) == {"file": file} and cards.send_kwargs(None) == {}


# ---------------------------------------------------------------- shop and sell

def test_shop_message_has_the_banner_and_no_plain_balance_field():
    view = shop_views.ShopView(7, None, 500)
    edit, send = run(view.message(edit=True)), run(view.message(edit=False))
    assert len(edit["attachments"]) == 1 and "file" in send
    assert text("shop.balance") not in [f.name for f in edit["embed"].fields]
    assert shop_views.shop_embed(500).fields[0].name == text("shop.balance")  # plain embed unchanged


def test_shop_choose_and_buy_redraw_the_banner_with_the_new_balance(monkeypatch):
    rec = Recorder()
    gate_ok(monkeypatch, shop_views, rec)
    key = next(iter(CATALOG))
    view = shop_views.ShopView(7, None, 500)
    inter = make_interaction(rec)
    inter.data = {"values": [key]}
    edits = edits_to(inter)
    run(view._choose(inter))
    assert len(edits[0]["attachments"]) == 1

    async def purchase(*_a, **_k):
        return SimpleNamespace(ok=True, reason=None, coins_left=123, item_key=key, quantity=1, total=377)

    monkeypatch.setattr(shop_views, "purchase", purchase)
    view = shop_views.ShopView(7, None, 500, selected=key)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._buy(inter, 1))
    assert view.coins == 123 and len(edits) == 1 and len(edits[0]["attachments"]) == 1
    assert edits[0]["attachments"][0].fp.getvalue() == cards._cached_banner("SHOP", 123)


def test_open_shop_sends_the_banner(monkeypatch):
    rec = Recorder()
    gate_ok(monkeypatch, shop_views, rec)
    monkeypatch.setattr(shop_views, "load_player", slow(rec, "load", SimpleNamespace(coins=500)))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    run(shop_views.open_shop(inter))
    assert len(sent) == 1 and sent[0]["file"].filename == cards.COIN_FILE


def test_open_wallet_sends_the_banner_and_keeps_the_history(monkeypatch):
    rec = Recorder()
    gate_ok(monkeypatch, shop_views, rec)
    entry = SimpleNamespace(coins=40, summary="Sold a creature", at=datetime.now(timezone.utc))
    monkeypatch.setattr(shop_views, "load_wallet", slow(rec, "load", Wallet(coins=900, entries=(entry,))))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    run(shop_views.open_wallet(inter))
    names = [f.name for f in sent[0]["embed"].fields]
    assert sent[0]["file"].filename == cards.COIN_FILE and names == [text("wallet.recent")]


def test_sell_message_and_screen_edits_carry_the_banner(monkeypatch):
    view = sell_views.SellView(7, None, 800, [], 0)
    edit = run(view.message(edit=True))
    assert len(edit["attachments"]) == 1 and text("sell.balance") not in [f.name for f in edit["embed"].fields]
    rec = Recorder()
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._cancel(inter))
    assert len(edits[0]["attachments"]) == 1


def test_open_sell_sends_the_banner(monkeypatch):
    rec = Recorder()
    gate_ok(monkeypatch, sell_views, rec)
    monkeypatch.setattr(sell_views, "load_player", slow(rec, "player", SimpleNamespace(coins=800)))
    monkeypatch.setattr(sell_views, "list_sellable", slow(rec, "rows", ([], 0, 0)))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    run(sell_views.open_sell(inter))
    assert sent[0]["file"].filename == cards.COIN_FILE


# ---------------------------------------------------------------- bag and daily

def test_open_inventory_sends_the_banner_without_the_plain_coin_field(monkeypatch):
    rec = Recorder()
    gate_ok(monkeypatch, items_views, rec)
    player = SimpleNamespace(coins=321, total_catches=4, daily_streak=2)
    monkeypatch.setattr(items_views, "ensure_starter_kit", slow(rec, "starter", False))
    monkeypatch.setattr(items_views, "load_inventory", slow(rec, "inv", {}))
    monkeypatch.setattr(items_views, "load_player", slow(rec, "player", player))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    run(items_views.open_inventory(inter))
    assert sent[0]["file"].filename == cards.COIN_FILE
    assert text("inventory.coins") not in [f.name for f in sent[0]["embed"].fields]


def daily_inter(monkeypatch, result):
    rec = Recorder()
    gate_ok(monkeypatch, items_views, rec)
    monkeypatch.setattr(items_views, "ensure_starter_kit", slow(rec, "starter", False))
    monkeypatch.setattr(items_views, "claim_daily", slow(rec, "claim", result))
    inter = make_interaction(rec)
    return inter, sends_to(inter)


def test_claimed_daily_sends_the_reward_card(monkeypatch):
    soon = datetime.now(timezone.utc) + timedelta(hours=20)
    inter, sent = daily_inter(monkeypatch, DailyResult(True, 3, soon, DailyReward(250, {BALL: 3})))
    run(items_views.open_daily(inter))
    assert sent[0]["file"].filename == cards.DAILY_FILE
    names = [f.name for f in sent[0]["embed"].fields]
    assert text("daily.items") not in names and text("inventory.coins") not in names
    assert sent[0]["file"].fp.getvalue() == cards._cached_daily(250, 3, ((items_views.item_name(BALL), 3),))


def test_daily_not_ready_stays_a_plain_embed(monkeypatch):
    soon = datetime.now(timezone.utc) + timedelta(hours=5)
    inter, sent = daily_inter(monkeypatch, DailyResult(False, 2, soon))
    run(items_views.open_daily(inter))
    assert "file" not in sent[0] and sent[0]["embed"].image.url is None


def test_sell_choose_and_page_turn_redraw_the_banner(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(sell_views, "list_sellable", slow(rec, "rows", ([], 0, 1)))
    view = sell_views.SellView(7, None, 800, [], 0)
    inter = make_interaction(rec)
    inter.data = {"values": [""]}
    edits = edits_to(inter)
    run(view._choose(inter))
    assert len(edits[0]["attachments"]) == 1
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._turn(inter, 1))
    assert len(edits[0]["attachments"]) == 1
