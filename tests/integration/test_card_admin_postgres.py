# path: tests/integration/test_card_admin_postgres.py
"""Card Hub admin screen + the card_economy admin helpers on real Postgres: owner-only (checked server-side on every click),
create limited / unlimited cards, offers, replenish, hide/show, and that what an admin makes is really buyable."""
import discord
import pytest

import config as app_config
from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from tests.integration.test_card_hub_postgres import FakeInteraction, GUILD, controls, text
from tests.integration.test_card_trade_ui_postgres import UI, _up as _up_base
from discord_bot.cogs import _views_card_admin as va
from discord_bot.cogs import _views_card_hub as h
from modules import card_economy as ce

ADMIN, USER = 1534574875274903562, 100000000000000009


async def _up_trade(pool):
    await _up_base(pool)
    async with pool.acquire() as c:        # the hub's home screen reads the global level
        await c.execute("CREATE TABLE IF NOT EXISTS discord_xp (guild_id BIGINT, user_id BIGINT, clone_id INTEGER, "
                        "total_xp BIGINT NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0)")


async def click(uid, act, arg="", value=None):
    fi = UI(uid)
    await h.dispatch(fi, act, None, GUILD, arg, value)
    return fi


@pytest.mark.asyncio
async def test_non_owner_gets_nothing_and_nav_hides_the_screen(pool):
    await _up_trade(pool)
    for act, arg in (("admin", ""), ("admnew", ""), ("admtog", "c1"), ("admcard", "")):
        fi = await click(USER, act, arg, "1" if act == "admcard" else None)
        assert "bot owner" in text(fi.view), act and fi.modal is None
    nav = next(c for c in controls((await click(USER, "home")).view) if c.custom_id.startswith("chubs:nav"))
    assert "admin" not in [o.value for o in nav.item.options]
    nav2 = next(c for c in controls((await click(ADMIN, "home")).view) if c.custom_id.startswith("chubs:nav"))
    assert "admin" in [o.value for o in nav2.item.options]


@pytest.mark.asyncio
async def test_create_limited_card_buy_out_replenish_and_hide(pool):
    await _up_trade(pool)
    r = await ce.create_card("Aurora Drake", "god", 900, 700, stock_total=2, actor_id=ADMIN)
    store = await ce.list_store(None, 10, 0)
    assert [c["name"] for c in store] == ["Aurora Drake"] and store[0]["stock_remaining"] == 2
    await _fund(pool, USER, 10_000)
    await ce.buy_offer(USER, None, r["offer_id"], 1)
    await ce.buy_offer(USER, None, r["offer_id"], 1)
    with pytest.raises(ce.StoreError):
        await ce.buy_offer(USER, None, r["offer_id"], 1)           # sold out
    assert await _bal(pool, USER) == 10_000 - 1400
    scr = await click(ADMIN, "admcard", "", str(r["card_id"]))
    assert "OUT OF STOCK" in text(scr.view)
    assert await ce.replenish_stock(r["card_id"], 3, ADMIN) == 3
    await ce.buy_offer(USER, None, r["offer_id"], 1)
    hid = await click(ADMIN, "admtog", f"c{r['card_id']}")
    assert "hidden" in text(hid.view) and await ce.list_store(None, 10, 0) == []
    shown = await click(ADMIN, "admtog", f"c{r['card_id']}")
    assert "in store" in text(shown.view) and len(await ce.list_store(None, 10, 0)) == 1


@pytest.mark.asyncio
async def test_validation_and_rental_rules(pool):
    await _up_trade(pool)
    for kw in ({"name": "x"}, {"rarity": "mythic"}, {"base_strength": -1}, {"price": -5}, {"stock_total": 0}):
        args = dict(name="Valid Card", rarity="rare", base_strength=10, price=10, stock_total=None) | kw
        with pytest.raises(ce.StoreError):
            await ce.create_card(**args)
    await ce.create_card("Dup Card", "rare", 10, 10)
    with pytest.raises(ce.StoreError) as e:
        await ce.create_card("dup card", "rare", 10, 10)
    assert e.value.code == "duplicate"
    unl = await ce.create_card("Rentable", "common", 5, 50)
    await ce.create_offer(unl["card_id"], "rental", 9, 7)               # unlimited cards can be rented
    lim = await ce.create_card("Ltd", "epic", 5, 50, stock_total=5)
    with pytest.raises(ce.StoreError):
        await ce.create_offer(lim["card_id"], "rental", 9, 7)           # limited cards can't
    assert await ce.get_card_admin(999999) is None


@pytest.mark.asyncio
async def test_admin_modals_and_screens_are_persistent_v2(pool, monkeypatch):
    await _up_trade(pool)
    fi = UI(ADMIN)
    m = va.NewCardModal(None, GUILD)
    m.name._value, m.rarity._value, m.strength._value, m.price._value, m.stock._value = "Modal Card", "epic", "250", "1,200", "10"
    await m.on_submit(fi)
    assert "Card created" in text(fi.view)
    card = (await ce.list_catalog())[0]
    assert (card["name"], card["stock_total"], card["base_value"]) == ("Modal Card", 10, 1200)
    bad = UI(ADMIN)
    m2 = va.NewCardModal(None, GUILD)
    m2.name._value, m2.rarity._value, m2.strength._value, m2.price._value, m2.stock._value = "Other", "epic", "abc", "5", ""
    await m2.on_submit(bad)
    assert bad.view is None and "whole number" in bad.messages[-1]
    off = UI(USER)                                                       # a non-owner submitting the modal is refused
    m3 = va.NewCardModal(None, GUILD)
    m3.name._value, m3.rarity._value, m3.strength._value, m3.price._value, m3.stock._value = "Sneaky", "epic", "1", "1", ""
    await m3.on_submit(off)
    assert off.view is None and not [c for c in await ce.list_catalog() if c["name"] == "Sneaky"]
    tpls = {cls.__discord_ui_compiled_template__: cls for cls in h.DYNAMIC_ITEMS}
    for act, arg, val in (("admin", "", None), ("admcard", "", str(card["card_id"]))):
        v = (await click(ADMIN, act, arg, val)).view
        assert isinstance(v, discord.ui.LayoutView) and v.has_components_v2() and v.timeout is None and v.is_persistent()
        assert v.total_children_count <= 40
        for c in controls(v):
            if c.custom_id.startswith("chub_pg:"):
                continue
            assert len(c.custom_id) <= 100 and any(t.fullmatch(c.custom_id) for t in tpls), c.custom_id
