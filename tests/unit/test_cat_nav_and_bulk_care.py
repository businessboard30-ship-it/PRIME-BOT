"""Cat menu navigation (one message, in place, with a Back button) and the Feed all / Water all / Sleep all buttons."""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import _views_cat_hub as hub
from modules import cat_adopt as ca
from modules import cat_care as cc
from modules import cat_pool, cat_wallet as cw
from tests.unit.test_cat_adopt import mkcat

NOW = datetime.now(timezone.utc)


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def no_images(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value="https://cdn.discordapp.com/a/b/c.jpg"))


def _count(view) -> int:
    return sum(1 for _ in view.walk_children())


def _card(rows=1, per_row=1, footer=True):
    view = discord.ui.LayoutView()
    box = discord.ui.Container()
    box.add_item(discord.ui.TextDisplay("hello"))
    for _ in range(rows):
        box.add_item(discord.ui.ActionRow(*[discord.ui.Button(label="x") for _ in range(per_row)]))
    if footer:
        box.add_item(discord.ui.TextDisplay("-# footer"))
    view.add_item(box)
    return view


def _inter(guild=1, message=True, done=False):
    i = MagicMock()
    i.guild_id = guild
    i.user.id = 7
    i.message = SimpleNamespace(id=555, components=[]) if message else None
    i.response.is_done = MagicMock(return_value=done)
    i.response.edit_message = AsyncMock()
    i.response.send_message = AsyncMock()
    i.edit_original_response = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _has_back(view) -> bool:
    return any(getattr(c, "custom_id", None) == vis.BACK_ID for c in view.walk_children())


# ── navigation ────────────────────────────────────────────────────────────
def test_screen_from_the_menu_replaces_the_menu_card_and_gets_a_back_button():
    i, view = _inter(), _card()
    token = vis.menu_origin.set({"picture_url": "https://x/cat.jpg"})
    try:
        run(vis.send(i, view=view))
    finally:
        vis.menu_origin.reset(token)
    i.response.edit_message.assert_awaited_once()
    i.response.send_message.assert_not_awaited()
    assert _has_back(i.response.edit_message.call_args.kwargs["view"])
    assert vis.hub_picture(555) == "https://x/cat.jpg"


def test_deferred_screen_from_the_menu_edits_the_original_message():
    i = _inter(done=True)
    token = vis.menu_origin.set({"picture_url": None})
    try:
        run(vis.send(i, view=_card(), deferred=True))
    finally:
        vis.menu_origin.reset(token)
    i.edit_original_response.assert_awaited_once()
    i.followup.send.assert_not_awaited()


def test_without_the_menu_nothing_changes():
    i = _inter()
    run(vis.send(i, view=_card()))
    i.response.send_message.assert_awaited_once()
    i.response.edit_message.assert_not_awaited()


def test_private_screens_never_replace_the_public_menu():
    i = _inter()
    token = vis.menu_origin.set({"picture_url": None})
    try:
        run(vis.send(i, view=_card(), public=False))
    finally:
        vis.menu_origin.reset(token)
    i.response.send_message.assert_awaited_once()
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True
    i.response.edit_message.assert_not_awaited()


def test_back_button_stays_within_40_components_even_on_a_full_card():
    for rows, per_row in ((1, 1), (5, 5), (6, 5), (6, 4)):
        view = _card(rows, per_row)
        before = _count(view)
        vis.attach_back(view)
        assert _count(view) <= 40 or before > 40, (rows, per_row)
        if before + 2 <= 40:
            assert _has_back(view)
    full = _card(6, 5, footer=False)
    vis.attach_back(full)
    assert _count(full) <= 40


def test_edit_keeps_the_back_button_on_screens_opened_from_the_menu():
    i = _inter()
    i.message = SimpleNamespace(id=1, components=[SimpleNamespace(children=[SimpleNamespace(custom_id=vis.BACK_ID)])])
    view = _card()
    run(vis.edit(i, view=view))
    assert _has_back(i.response.edit_message.call_args.kwargs["view"])
    plain = _inter()
    view2 = _card()
    run(vis.edit(plain, view=view2))
    assert not _has_back(view2)


def test_edit_uses_the_original_message_when_asked():
    i = _inter(done=True)
    run(vis.edit(i, original=True, view=_card()))
    i.edit_original_response.assert_awaited_once()
    i.response.edit_message.assert_not_awaited()


def test_menu_choice_sets_the_origin_for_the_screen_it_opens(monkeypatch):
    seen = []

    class Fake:
        async def callback(self, interaction):
            seen.append(vis.menu_origin.get())

    monkeypatch.setattr(hub, "_button_for", lambda key: Fake())
    menu = hub.HubView("https://x/cat.jpg")
    select = next(c for c in menu.walk_children() if isinstance(c, hub.HubSelect))
    monkeypatch.setattr(type(select), "values", property(lambda self: ["mine"]))
    run(select.callback(_inter()))
    assert seen == [{"picture_url": "https://x/cat.jpg"}] and vis.menu_origin.get() is None


def test_back_to_menu_rebuilds_the_menu_with_its_cat_picture(monkeypatch):
    vis.remember_hub(777, "https://x/cat.jpg")
    monkeypatch.setattr(hub, "status", AsyncMock(return_value=[]))
    monkeypatch.setattr(hub, "_can_setup", AsyncMock(return_value=False))
    i = _inter()
    i.message = SimpleNamespace(id=777, components=[])
    run(hub.BackToMenu().callback(i))
    shown = i.response.edit_message.call_args.kwargs["view"]
    assert isinstance(shown, hub.HubView) and not _has_back(shown)
    assert any(isinstance(c, discord.ui.MediaGallery) for c in shown.walk_children())


def test_my_cats_card_with_back_and_bulk_row_fits(monkeypatch):
    monkeypatch.setattr(cw, "safe_wallet", AsyncMock(return_value=cw.Wallet(500, 2)))
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1), mkcat(2), mkcat(3)])
    run(view.refresh_wallet()); run(view.draw())
    vis.attach_back(view)
    assert _has_back(view) and 20 <= _count(view) <= 40


# ── bulk care ─────────────────────────────────────────────────────────────
def test_target_count_follows_the_single_cat_rules():
    hungry = dataclasses.replace(mkcat(1), hungry_since=NOW)
    watered = dataclasses.replace(mkcat(2), watered_until=NOW + timedelta(hours=20))
    nearly_out = dataclasses.replace(mkcat(3), watered_until=NOW + timedelta(hours=2))
    cats = [hungry, watered, nearly_out, mkcat(4)]
    assert cc.bulk_target_count(cats, "feed") == 1
    assert cc.bulk_target_count(cats, "water") == 3          # all but the one with more than 12 h left
    assert cc.bulk_target_count(cats, "bed") == 4


def test_bulk_care_charges_each_cat_and_stops_when_coins_run_out(monkeypatch):
    monkeypatch.setattr(cc, "_candidates", AsyncMock(return_value=[1, 2, 3, 4]))
    calls = []

    async def feed(cat_id, owner, clone):
        calls.append(cat_id)
        if cat_id == 3:
            return cc.CareResult("poor", coins=10, cost=80)
        return cc.CareResult("ok", coins=500 - 80 * len(calls), cost=80)
    monkeypatch.setattr(cc, "feed", feed)
    res = run(cc.bulk_care(9, None, "feed"))
    assert (res.done, res.spent, res.short, res.coins) == (2, 160, 2, 10)
    assert calls == [1, 2, 3]                                 # it stopped at the first cat it couldn't pay for


def test_bulk_care_skips_cats_that_changed_and_survives_errors(monkeypatch):
    monkeypatch.setattr(cc, "_candidates", AsyncMock(return_value=[1, 2, 3]))

    async def water(cat_id, owner, clone):
        if cat_id == 1:
            return cc.CareResult("already", cost=30)
        if cat_id == 2:
            raise RuntimeError("boom")
        return cc.CareResult("ok", coins=100, cost=30)
    monkeypatch.setattr(cc, "water", water)
    res = run(cc.bulk_care(9, None, "water"))
    assert (res.done, res.spent, res.skipped, res.short) == (1, 30, 2, 0)


def _view(monkeypatch, cats):
    monkeypatch.setattr(cw, "safe_wallet", AsyncMock(return_value=cw.Wallet(500, 0)))
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), cats)
    run(view.refresh_wallet()); view._build()
    return view


def _bulk_buttons(view):
    return {b.label: b for b in view.walk_children() if isinstance(b, discord.ui.Button)
            and str(b.label).split(" (")[0] in ("Feed all", "Water all", "Sleep all")}


def test_all_buttons_show_how_many_cats_and_disable_at_zero(monkeypatch):
    hungry = dataclasses.replace(mkcat(1), hungry_since=NOW)
    b = _bulk_buttons(_view(monkeypatch, [hungry, mkcat(2)]))
    assert set(b) == {"Feed all (1)", "Water all (2)", "Sleep all (2)"}
    assert not any(x.disabled for x in b.values())
    b = _bulk_buttons(_view(monkeypatch, [mkcat(1), mkcat(2)]))
    assert b["Feed all (0)"].disabled and not b["Water all (2)"].disabled


def test_first_tap_shows_the_exact_cost_and_the_second_tap_does_it(monkeypatch):
    hungry = [dataclasses.replace(mkcat(1), hungry_since=NOW), dataclasses.replace(mkcat(2), hungry_since=NOW)]
    view = _view(monkeypatch, hungry)
    view.trade_id = "T1"
    monkeypatch.setattr(cc, "bulk_preview", AsyncMock(return_value=(2, 160)))
    bulk = AsyncMock(return_value=cc.BulkResult("feed", done=2, spent=160, coins=340))
    monkeypatch.setattr(cc, "bulk_care", bulk)
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1), mkcat(2)]))
    i = _inter(message=False)
    run(view._bulk(i, "feed"))
    bulk.assert_not_awaited()                                  # first tap: nothing spent yet
    text = " ".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "2 cats" in text and "160 coins" in text and "again" in text
    assert any(str(b.label).startswith("Tap again") for b in _bulk_buttons_all(view))
    run(view._bulk(i, "feed"))
    bulk.assert_awaited_once_with(7, None, "feed")
    text = " ".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "Fed **2 cats**" in text and "340 left" in text and view.confirm_bulk is None


def _bulk_buttons_all(view):
    return [b for b in view.walk_children() if isinstance(b, discord.ui.Button)]


def test_nothing_to_do_says_so_and_spends_nothing(monkeypatch):
    view = _view(monkeypatch, [mkcat(1)])
    monkeypatch.setattr(cc, "bulk_preview", AsyncMock(return_value=(0, 0)))
    bulk = AsyncMock()
    monkeypatch.setattr(cc, "bulk_care", bulk)
    run(view._bulk(_inter(message=False), "bed"))
    bulk.assert_not_awaited()
    assert "None of your cats need that" in " ".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def test_any_other_action_cancels_a_waiting_confirm(monkeypatch):
    view = _view(monkeypatch, [mkcat(1)])
    view.confirm_bulk = ("feed", "1 cat", 80)
    run(view._push(_inter(message=False), "something else"))
    assert view.confirm_bulk is None
