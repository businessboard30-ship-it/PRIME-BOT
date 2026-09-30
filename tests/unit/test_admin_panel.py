"""Owner-panel wizard tests (real discord.py components, stubbed DB/config)."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

OWNER, OTHER = 111, 222
MOD = "discord_bot.cogs._views_admin_panel"


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    dbm.db.list_active_discord_clones = AsyncMock(return_value=[
        {"clone_id": 7, "bot_username": "CloneSeven"}, {"clone_id": 9, "bot_username": "CloneNine"}])
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(MOD, None)
    mod = importlib.import_module(MOD)
    yield mod
    sys.modules.pop(MOD, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, guild=None, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = guild
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def cog():
    c = MagicMock()
    c.clone_admin.approvepayment = AsyncMock()
    c.clone_admin.rejectpayment = AsyncMock()
    c.clone_admin.assignpayment = AsyncMock()
    c.clone_admin.pendingpayments = AsyncMock()
    c.clone_admin.paymentmode = AsyncMock()
    c.clone_admin.ownerbroadcast = AsyncMock()
    c.clone_admin.broadcaststatus = AsyncMock()
    return c


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def count(view):
    return sum(1 for _ in view.walk_children())


async def _view_in_loop(factory):
    return factory()


def test_home_render_and_limits(vp):
    async def go():
        v = vp.HomeView(cog(), OWNER)
        b = buttons(v)
        assert {"Payments", "Broadcast", "Close"} <= set(b)
        assert not b["Payments"].disabled and not b["Broadcast"].disabled
        assert count(v) < 40
    run(go())


def test_sections_disabled_without_access(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp, "DISCORD_OWNER_BROADCAST_IDS", set())
        v = vp.HomeView(cog(), OWNER)
        assert buttons(v)["Broadcast"].disabled
        assert not buttons(v)["Payments"].disabled
    run(go())


def test_only_opener_and_still_authorized(vp, monkeypatch):
    async def go():
        v = vp.HomeView(cog(), OWNER)
        i = I(user=OTHER)
        assert await v.interaction_check(i) is False
        i.response.send_message.assert_awaited()
        i2 = I(user=OWNER)
        assert await v.interaction_check(i2) is True
        monkeypatch.setattr(vp, "DISCORD_CLONE_ADMIN_IDS", set())
        monkeypatch.setattr(vp, "DISCORD_OWNER_BROADCAST_IDS", set())
        assert await v.interaction_check(I(user=OWNER)) is False   # revoked mid-session
    run(go())


def test_navigation_home_to_payments_and_back(vp):
    async def go():
        c = cog(); v = vp.HomeView(c, OWNER); i = I()
        await buttons(v)["Payments"].callback(i)
        nxt = i.response.edit_message.await_args.kwargs["view"]
        assert isinstance(nxt, vp.PaymentsView)
        i2 = I()
        await buttons(nxt)["Back"].callback(i2)
        assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.HomeView)
    run(go())


def test_approve_modal_passes_float_and_blank(vp):
    async def go():
        c = cog()
        m = vp.ReferenceModal(c.clone_admin, "approve")
        m.reference._value = " ref_1 "; m.amount._value = "1,250.50"
        i = I(); await m.on_submit(i)
        c.clone_admin.approvepayment.assert_awaited_once_with(i, reference="ref_1", amount=1250.5)
        c.clone_admin.approvepayment.reset_mock()
        m.amount._value = ""
        await m.on_submit(I())
        assert c.clone_admin.approvepayment.await_args.kwargs["amount"] is None
    run(go())


def test_approve_modal_rejects_bad_amount_without_calling(vp):
    async def go():
        c = cog(); m = vp.ReferenceModal(c.clone_admin, "approve")
        m.reference._value = "r"; m.amount._value = "abc"
        i = I(); await m.on_submit(i)
        c.clone_admin.approvepayment.assert_not_awaited()
        assert "number" in i.response.send_message.await_args.args[0]
    run(go())


def test_reject_and_assign_modals(vp):
    async def go():
        c = cog()
        r = vp.ReferenceModal(c.clone_admin, "reject"); r.reference._value = "x"
        i = I(); await r.on_submit(i)
        c.clone_admin.rejectpayment.assert_awaited_once_with(i, reference="x")
        a = vp.ReferenceModal(c.clone_admin, "assign"); a.reference._value = "y"; a.server_id._value = "123"
        i2 = I(); await a.on_submit(i2)
        c.clone_admin.assignpayment.assert_awaited_once_with(i2, reference="y", server_id="123")
    run(go())


def test_pending_queue_button_delegates(vp):
    async def go():
        c = cog(); v = vp.PaymentsView(c, OWNER, "payments"); i = I()
        await buttons(v)["Pending queue"].callback(i)
        c.clone_admin.pendingpayments.assert_awaited_once_with(i)
    run(go())


def test_payment_mode_two_step_and_args(vp):
    async def go():
        c = cog()
        v = vp.PaymentModeView(c, OWNER, "payments", await vp.db.list_active_discord_clones())
        assert buttons(v)["Apply"].disabled                      # no mode chosen yet
        await v._pick_mode(I(values=["gumroad"]))
        await v._pick_scope(I(values=["clone:7"]))
        assert not buttons(v)["Apply"].disabled
        i1 = I(); await buttons(v)["Apply"].callback(i1)        # first press only asks to confirm
        c.clone_admin.paymentmode.assert_not_awaited()
        assert "Confirm" in buttons(v)
        i2 = I(); await buttons(v)["Confirm"].callback(i2)
        c.clone_admin.paymentmode.assert_awaited_once()
        kw = c.clone_admin.paymentmode.await_args.kwargs
        assert kw["clone_id"] == 7 and kw["mode"].value == "gumroad" and "all_clones" not in kw
        # 'all clones' scope
        c.clone_admin.paymentmode.reset_mock()
        await v._pick_scope(I(values=["all"])); await buttons(v)["Apply"].callback(I()); await buttons(v)["Confirm"].callback(I())
        assert c.clone_admin.paymentmode.await_args.kwargs["all_clones"] is True
    run(go())


def test_payment_mode_changing_a_choice_cancels_confirm(vp):
    async def go():
        v = vp.PaymentModeView(cog(), OWNER, "payments", [])
        await v._pick_mode(I(values=["auto"]))
        await buttons(v)["Apply"].callback(I())
        assert v._confirm
        await v._pick_scope(I(values=["all"]))
        assert not v._confirm
    run(go())


def test_broadcast_blocked_in_guild(vp):
    async def go():
        v = vp.BroadcastView(cog(), OWNER, "broadcast", in_dm=False)
        b = buttons(v)
        assert "Send…" not in b and "Write / edit message" not in b
        assert "Delivery status" in b and selects(v) == []
    run(go())


def test_broadcast_flow_send_once(vp):
    async def go():
        c = cog(); v = vp.BroadcastView(c, OWNER, "broadcast", in_dm=True)
        await v._ensure_clones(); v._build()
        assert buttons(v)["Send…"].disabled                       # no text yet
        assert len(selects(v)) == 2 and count(v) < 40
        v.message = "Hello world"; v._build()
        await v._pick_target(I(values=["servers"])); await v._pick_clone(I(values=["9"]))
        i1 = I(); await buttons(v)["Send…"].callback(i1)
        c.clone_admin.ownerbroadcast.assert_not_awaited()
        assert "Confirm & send" in buttons(v)
        i2 = I(); await buttons(v)["Confirm & send"].callback(i2)
        c.clone_admin.ownerbroadcast.assert_awaited_once()
        kw = c.clone_admin.ownerbroadcast.await_args.kwargs
        assert kw["message"] == "Hello world" and kw["target"].value == "servers" and kw["clone"] == "9"
        # stale second click must not re-send
        i3 = I(); await v._send(i3)
        c.clone_admin.ownerbroadcast.assert_awaited_once()
        assert "already queued" in i3.response.send_message.await_args.args[0]
    run(go())


def test_broadcast_admins_with_clone_cannot_send(vp):
    async def go():
        v = vp.BroadcastView(cog(), OWNER, "broadcast", in_dm=True)
        v.message = "hi"; v.target = "admins"; v.clone_id = 7; v._build()
        assert buttons(v)["Send…"].disabled
    run(go())


def test_broadcast_attachment_is_forwarded(vp):
    async def go():
        c = cog(); v = vp.BroadcastView(c, OWNER, "broadcast", in_dm=True)
        att = MagicMock(spec=discord.Attachment); att.filename = "pic.png"
        v.message = "m"; v.attachment = att; v._build()
        await buttons(v)["Send…"].callback(I()); await buttons(v)["Confirm & send"].callback(I())
        assert c.clone_admin.ownerbroadcast.await_args.kwargs["attachment"] is att
    run(go())


def test_compose_modal_builds_and_serializes(vp):
    async def go():
        v = vp.BroadcastView(cog(), OWNER, "broadcast", in_dm=True)
        m = vp.BroadcastComposeModal(v)
        payload = m.to_components()
        assert len(payload) == 2                                   # text input + file-upload label
        assert payload[1]["type"] == 18                            # Label
        m.message._value = "typed"
        i = I(); await m.on_submit(i)
        assert v.message == "typed"
        i.response.edit_message.assert_awaited()
    run(go())


def test_soft_refresh_never_raises(vp):
    async def go():
        v = vp.HomeView(cog(), OWNER); i = I()
        i.followup.edit_message = AsyncMock(side_effect=RuntimeError("boom"))
        await v.soft_refresh(i)          # must swallow
    run(go())
