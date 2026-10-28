# path: tests/integration/test_card_chief_ui_postgres.py
"""The 👑 Chief Seats hub screen driven end to end with fake Discord interactions on a real Postgres: Components V2 only,
no timeout, every control rebuilt from its custom_id alone (a restart), confirm step, double-click safety, stale states."""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import config as app_config
from discord_bot.cogs import _views_card_hub as h
from tests.integration.test_card_chief_postgres import G, _fund, _bal, _seat, _up, _xp  # noqa: F401
from tests.integration.test_card_economy_postgres import pg_uri, pool  # noqa: F401
from tests.integration.test_card_hub_postgres import FakeInteraction, controls, text


class Chan:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


async def click(uid, act, arg="", value=None, chan=None):
    fi = FakeInteraction(uid)
    fi.channel = chan if chan is not None else Chan()
    await h.dispatch(fi, act, None, G, arg, value)
    return fi


@pytest.mark.asyncio
async def test_screen_is_v2_lists_five_seats_and_is_in_the_nav(pool):
    await _up(pool)
    fi = await click(1, "nav", "", "chief")
    v = fi.view
    assert isinstance(v, discord.ui.LayoutView) and v.has_components_v2() and v.timeout is None
    t = text(v)
    assert "Chief Seats" in t and all(f"**{k}." in t for k in range(1, 6))
    nav = next(c for c in controls(v) if c.custom_id.startswith("chubs:nav"))
    assert any(o.value == "chief" and o.default for o in nav.item.options)
    assert any(c.custom_id.startswith("chubs:chiefpick") for c in controls(v))


@pytest.mark.asyncio
async def test_pick_confirm_buy_flow_charges_once_and_announces(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 100_000)
    fi = await click(1, "chiefpick", "", "2")
    assert "Lease seat 2" in text(fi.view) and f"{app_config.CHIEF_SEAT_PRICES[1]:,}" in text(fi.view)
    assert await _bal(pool, 1) == 100_000                                       # confirm screen charges nothing
    chan = Chan()
    fi = await click(1, "chiefok", "s2", chan=chan)
    assert "Leased seat 2" in text(fi.view) and await _bal(pool, 1) == 100_000 - app_config.CHIEF_SEAT_PRICES[1]
    assert (await _seat(pool, 2))["user_id"] == 1
    assert chan.sent and "<@1>" in chan.sent[0]


@pytest.mark.asyncio
async def test_double_click_on_confirm_charges_once(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 100_000)
    await asyncio.gather(click(1, "chiefok", "s3"), click(1, "chiefok", "s3"))
    assert await _bal(pool, 1) == 100_000 - app_config.CHIEF_SEAT_PRICES[2]


@pytest.mark.asyncio
async def test_failures_show_a_friendly_note_and_keep_the_hub_usable(pool):
    await _up(pool)
    await _xp(pool, 1, level=1)
    await _xp(pool, 2)
    await _fund(pool, 1, 999_999)
    await _fund(pool, 2, 5)
    fi = await click(1, "chiefok", "s1")
    assert f"level {app_config.CHIEF_MIN_LEVEL}" in text(fi.view) and await _bal(pool, 1) == 999_999
    fi = await click(2, "chiefok", "s1")
    assert "Not enough coins" in text(fi.view) and await _bal(pool, 2) == 5
    assert any(c.custom_id.startswith("chub:chief:") for c in controls(fi.view))      # a Back button is still there
    fi = await click(2, "chiefpick", "", "99")                                         # stale / forged seat number
    assert "doesn't exist" in text(fi.view)


@pytest.mark.asyncio
async def test_someone_elses_lease_is_shown_and_cannot_be_confirmed(pool):
    await _up(pool)
    for u in (1, 2):
        await _xp(pool, u)
        await _fund(pool, u, 100_000)
    await click(1, "chiefok", "s1")
    fi = await click(2, "chiefpick", "", "1")
    assert "leased right now" in text(fi.view) and not any(c.custom_id.startswith("chub:chiefok") for c in controls(fi.view))
    fi = await click(2, "chiefok", "s1")                                               # forged confirm click
    assert "leased" in text(fi.view).lower() and await _bal(pool, 2) == 100_000
    fi = await click(2, "chief")
    assert "leased until" in text(fi.view)


@pytest.mark.asyncio
async def test_every_chief_control_survives_a_restart_and_has_no_timeout(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 100_000)
    shots = [(await click(1, "chief")).view, (await click(1, "chiefpick", "", "4")).view]
    tpls = {cls.__discord_ui_compiled_template__: cls for cls in h.DYNAMIC_ITEMS}
    seen = 0
    for view in shots:
        assert view.has_components_v2() and view.timeout is None and view.is_persistent()
        assert not any(isinstance(c, discord.ui.Button) and c.url for c in view.walk_children())
        for c in controls(view):
            cid = c.custom_id
            assert len(cid) <= 100
            m = next((t.fullmatch(cid) for t in tpls if t.fullmatch(cid)), None)
            assert m, f"{cid} would not be routed after a restart"
            rebuilt = await tpls[m.re].from_custom_id(FakeInteraction(1), None, m)      # fresh process: custom_id only
            assert rebuilt.custom_id == cid
            seen += 1
    assert seen >= 4
    # the rebuilt confirm button really works against the database after the "restart"
    cid = next(c.custom_id for c in controls(shots[1]) if c.custom_id.startswith("chub:chiefok"))
    m = next(t.fullmatch(cid) for t in tpls if t.fullmatch(cid))
    rebuilt = await tpls[m.re].from_custom_id(FakeInteraction(1), None, m)
    fi = FakeInteraction(1)
    fi.channel = Chan()
    await rebuilt.callback(fi)
    assert (await _seat(pool, 4))["user_id"] == 1
