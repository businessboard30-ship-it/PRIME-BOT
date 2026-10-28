# path: tests/integration/test_card_trade_ui_postgres.py
"""The Card Hub's 💱 Trade & games screen against real Postgres with fake Discord interactions: global + local trades built
from a DB-backed draft, accept / decline / cancel, coin transfers with a confirm step, the three games, and the hub's
persistence guarantees (every control rebuilt from its custom_id alone, Components V2, no timeout, component ceiling)."""
from types import SimpleNamespace

import discord
import pytest

from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from tests.integration.test_card_hub_postgres import FakeInteraction, GUILD, controls, text
from discord_bot.cogs import _views_card_hub as h
from discord_bot.cogs import _views_card_trade as vt
from modules import card_economy as ce
from modules import card_trade as ct

DIR = MIGRATION.parent
A, B, C = 100000000000000001, 100000000000000002, 100000000000000003


async def _up(pool):
    async with pool.acquire() as c:
        for f in ("089_card_boosts.sql", "090_card_trading.sql", "091_card_coin_games.sql", "092_card_profiles.sql",
                  "094_card_trade_drafts.sql"):
            await c.execute((DIR / f).read_text())


class UI(FakeInteraction):
    """Fake interaction that also knows who is in which guild (for local-trade membership checks)."""
    def __init__(self, uid, members=(A, B)):
        super().__init__(uid, known=[SimpleNamespace(id=i, display_name=f"u{i}", bot=False) for i in (A, B, C)])
        guild = SimpleNamespace(id=GUILD, get_member=lambda i: SimpleNamespace(id=i, display_name=f"u{i}") if i in members else None,
                                fetch_member=self._fm(members))
        self.guild = guild
        self.client.get_guild = lambda gid: guild if gid == GUILD else None

    @staticmethod
    def _fm(members):
        async def fetch(i):
            if i in members:
                return SimpleNamespace(id=i)
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "no member")
        return fetch


async def click(uid, act, arg="", value=None, members=(A, B)):
    fi = UI(uid, members)
    await h.dispatch(fi, act, None, GUILD, arg, value)
    return fi


async def _card(pool, owner, name="Card", strength=100, tradeable=True):
    await _up(pool)
    async with pool.acquire() as c:
        cid = await c.fetchval("INSERT INTO card_catalog (name, rarity, base_strength, tradeable) VALUES ($1,'rare',$2,$3) "
                               "RETURNING card_id", name, strength, tradeable)
        return await c.fetchval("INSERT INTO card_copies (card_id, owner_id) VALUES ($1,$2) RETURNING copy_id", cid, owner)


async def _owner(pool, copy):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT owner_id FROM card_copies WHERE copy_id=$1", copy)


def _select(view, prefix):
    return next(c for c in controls(view) if c.custom_id.startswith(prefix))


@pytest.mark.asyncio
async def test_global_trade_from_draft_to_accept_swaps_cards(pool):
    mine, theirs = await _card(pool, A, "Mine"), await _card(pool, B, "Theirs")
    await ct.start_draft(A, None, B, "global")
    pick = await click(A, "tcards", "sy.p0")
    assert [o.value for o in _select(pick.view, "chubs:tadd").item.options] == [str(mine)]
    await click(A, "tadd", "y", str(mine))
    pick2 = await click(A, "tcards", "st.p0")
    assert [o.value for o in _select(pick2.view, "chubs:tadd").item.options] == [str(theirs)]
    await click(A, "tadd", "t", str(theirs))
    draft = await click(A, "tdraft")
    assert "Mine" in text(draft.view) and "Theirs" in text(draft.view)
    sent = await click(A, "tsend")
    assert "Offer #" in text(sent.view)
    tid = (await ct.list_pending(A, None))["trades"][0]["trade_id"]
    assert await ct.get_draft(A, None) is None
    theirs_screen = await click(B, "trade")
    assert "Accept" in " ".join(getattr(c.item, "label", "") or "" for c in controls(theirs_screen.view)) and "Mine" in text(theirs_screen.view)
    bad = await click(C, "tacc", f"t{tid}")
    assert "isn't addressed to you" in text(bad.view) and await _owner(pool, mine) == A
    ok = await click(B, "tacc", f"t{tid}")
    assert "complete" in text(ok.view)
    assert (await _owner(pool, mine), await _owner(pool, theirs)) == (B, A)
    again = await click(B, "tacc", f"t{tid}")
    assert "no longer open" in text(again.view)


@pytest.mark.asyncio
async def test_local_trade_needs_the_recipient_in_this_server(pool):
    mine, theirs = await _card(pool, A, "Mine"), await _card(pool, B, "Theirs")
    await ct.start_draft(A, None, B, "local", GUILD)
    await ct.draft_add_cards(A, None, "give", [mine])
    await ct.draft_add_cards(A, None, "get", [theirs])
    out = await click(A, "tsend", members=(A,))           # B has left the server
    assert "isn't a member" in text(out.view)
    assert await ct.get_draft(A, None) is not None         # a refused send keeps the draft
    ok = await click(A, "tsend", members=(A, B))
    assert "Offer #" in text(ok.view)
    tid = (await ct.list_pending(A, None))["trades"][0]["trade_id"]
    gone = await click(B, "tacc", f"t{tid}", members=(A,))   # B left before accepting
    assert "isn't a member" in text(gone.view) or "no longer a member" in text(gone.view)
    assert await _owner(pool, mine) == A


@pytest.mark.asyncio
async def test_decline_and_cancel_unlock_cards_and_draft_rejects_foreign_cards(pool):
    mine, theirs = await _card(pool, A, "Mine"), await _card(pool, B, "Theirs")
    await ct.start_draft(A, None, B, "global")
    await ct.draft_add_cards(A, None, "give", [theirs])            # not mine: silently ignored
    assert (await ct.get_draft(A, None))["give"] == []
    await ct.draft_add_cards(A, None, "give", [mine])
    await ct.draft_set_coins(A, None, 0, 50)
    await click(A, "tsend")
    tid = (await ct.list_pending(A, None))["trades"][0]["trade_id"]
    out = await click(B, "tdec", f"t{tid}")
    assert "closed" in text(out.view)
    async with pool.acquire() as c:
        assert not await c.fetchval("SELECT trade_locked FROM card_copies WHERE copy_id=$1", mine)
        assert await c.fetchval("SELECT status FROM card_trades WHERE trade_id=$1", tid) == "cancelled"
    with pytest.raises(ce.StoreError):
        await ct.draft_set_coins(A, None, 5, 5)


@pytest.mark.asyncio
async def test_untradeable_and_pick_lists_exclude_locked_cards(pool):
    ok, nope = await _card(pool, A, "Ok"), await _card(pool, A, "Soulbound", tradeable=False)
    await ct.start_draft(A, None, B, "global")
    rows = await ct.list_tradeable(A, None)
    assert [r["copy_id"] for r in rows] == [ok] and nope not in [r["copy_id"] for r in rows]


@pytest.mark.asyncio
async def test_coin_transfer_has_a_confirm_step_and_never_goes_negative(pool):
    await _up(pool)
    await _fund(pool, A, 100)
    conf = vt.screen_coin_confirm(h.Ctx(UI(A), None, GUILD), B, 60)
    assert "Send coins?" in text(conf) and await _bal(pool, A) == 100        # nothing moves before confirming
    ok = await click(A, "coinok", f"u{B}.a60")
    assert "Sent **60**" in text(ok.view) and (await _bal(pool, A), await _bal(pool, B)) == (40, 60)
    short = await click(A, "coinok", f"u{B}.a60")
    assert "⚠️" in text(short.view) and (await _bal(pool, A), await _bal(pool, B)) == (40, 60)
    me = await click(A, "coinok", f"u{A}.a5")
    assert "yourself" in text(me.view) and await _bal(pool, A) == 40


@pytest.mark.asyncio
async def test_games_move_coins_respect_cooldowns_and_show_results(pool):
    await _up(pool)
    await _fund(pool, A, 1000)
    first = await click(A, "gcfa", "w100")
    assert ("won" in text(first.view) or "lost" in text(first.view)) and await _bal(pool, A) in (900, 1100)
    again = await click(A, "gcfa", "w100")
    assert "Try again in" in text(again.view)                                   # cooldown, no second charge
    assert await _bal(pool, A) in (900, 1100)
    broke = await click(C, "gcfa", "w100")
    assert "⚠️" in text(broke.view) and (await _bal(pool, C) or 0) == 0
    lg = await click(A, "glga", "l0.w50")
    assert "League Flip" in text(lg.view)
    poor = await click(A, "grob", "")                                            # opens a modal, no crash
    assert poor.modal is not None


@pytest.mark.asyncio
async def test_modals_open_without_deferring_and_parse_input(pool):
    await _up(pool)
    for act, arg in (("tnew", "g"), ("tnew", "l"), ("coins", ""), ("gcf", "")):
        fi = await click(A, act, arg)
        assert fi.modal is not None and fi.view is None, act
    fi = UI(A)
    m = vt.NewTradeModal(None, GUILD, "global")
    m.who._value, m.give_coins._value, m.get_coins._value = str(B), "1,500", ""
    await m.on_submit(fi)
    assert "trade" in text(fi.view) and (await ct.get_draft(A, None))["give_coins"] == 1500
    bad = UI(A)
    m2 = vt.NewTradeModal(None, GUILD, "global")
    m2.who._value, m2.give_coins._value, m2.get_coins._value = str(B), "abc", ""
    await m2.on_submit(bad)
    assert bad.view is None and "whole number" in bad.messages[-1]


@pytest.mark.asyncio
async def test_trade_screens_are_persistent_v2_within_limits_and_survive_a_restart(pool):
    mine, theirs = await _card(pool, A, "Mine"), await _card(pool, B, "Theirs")
    await _fund(pool, A, 500)
    await ct.start_draft(A, None, B, "global")
    await ct.draft_add_cards(A, None, "give", [mine])
    await ct.draft_add_cards(A, None, "get", [theirs])
    await click(A, "tsend")
    await ct.start_draft(A, None, B, "global")
    shots = [(a, (await click(A, a, g, v)).view) for a, g, v in [
        ("trade", "", None), ("games", "", None), ("tdraft", "", None), ("tcards", "sy.p0", None), ("tcards", "st.p0", None)]]
    shots.append(("trade-as-recipient", (await click(B, "trade")).view))
    tpls = {cls.__discord_ui_compiled_template__: cls for cls in h.DYNAMIC_ITEMS}
    seen = 0
    for act, view in shots:
        assert isinstance(view, discord.ui.LayoutView) and view.has_components_v2(), act
        assert view.timeout is None and view.is_persistent(), act
        assert view.total_children_count <= 40, (act, view.total_children_count)
        for c in controls(view):
            cid = c.custom_id
            assert len(cid) <= 100, cid
            if cid.startswith("chub_pg:"):
                continue
            m = next((t.fullmatch(cid) for t in tpls if t.fullmatch(cid)), None)
            assert m, f"{cid} would not be routed after a restart"
            rebuilt = await tpls[m.re].from_custom_id(FakeInteraction(1), None, m)
            assert rebuilt.custom_id == cid and m["act"] in (vt.ACTS | {"nav", "home", "store", "col", "prof", "ranks"})
            seen += 1
    assert seen > 20
    dm = vt.offer_dm_view(h.Ctx(UI(A), None, GUILD), await ct.get_trade((await ct.list_pending(A, None))["trades"][0]["trade_id"]))
    assert dm.timeout is None and dm.has_components_v2() and len(controls(dm)) == 2


@pytest.mark.asyncio
async def test_a_stale_offer_button_after_expiry_is_graceful_and_the_hub_still_works(pool):
    mine, theirs = await _card(pool, A, "Mine"), await _card(pool, B, "Theirs")
    t = await ct.propose_trade(A, B, None, "global", [mine], [theirs])
    async with pool.acquire() as c:
        await c.execute("UPDATE card_trades SET expires_at = NOW() - interval '1 minute' WHERE trade_id=$1", t)
    out = await click(B, "tacc", f"t{t}")
    assert "no longer open" in text(out.view) or "expired" in text(out.view)
    assert await _owner(pool, mine) == A
    assert "Trade & games" in text((await click(B, "trade")).view)
    swept = await ct.expire_trades()
    assert swept == 1
    assert await click(A, "tdec", f"t{t}")                                      # old Cancel button: friendly, not a crash
