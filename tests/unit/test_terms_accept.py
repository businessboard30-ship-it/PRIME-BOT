"""Terms & Privacy wizard posted when the bot joins a server."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_terms as v
from modules import terms_accept as ta


def run(c):
    return asyncio.run(c)


def perms(**kw):
    base = dict(manage_guild=False, administrator=False, view_channel=True, send_messages=True, embed_links=True)
    base.update(kw)
    return SimpleNamespace(**base)


def member(uid=10, owner_id=99, **kw):
    guild = SimpleNamespace(owner_id=owner_id)
    return SimpleNamespace(id=uid, guild=guild, guild_permissions=perms(**kw))


class Conn:
    def __init__(self, existing=None):
        self.row = existing
    async def fetchrow(self, sql, *a):
        if sql.startswith("INSERT"):
            if self.row:
                return None
            self.row = {"accepted_by": a[2], "accepted_at": datetime(2026, 10, 12, tzinfo=timezone.utc), "terms_version": a[3]}
            return self.row
        return self.row


def use(monkeypatch, conn):
    class Acq:
        async def __aenter__(self): return conn
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(ta, "_pool", AsyncMock(return_value=SimpleNamespace(acquire=lambda: Acq())))
    return conn


class FI:
    def __init__(self, user, guild_id=5, clone_id=None):
        self.user, self.guild_id = user, guild_id
        self.guild = SimpleNamespace(name="Cat Club", id=guild_id, leave=AsyncMock())
        self.client = SimpleNamespace(clone_id=clone_id)
        self.response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        self.followup = SimpleNamespace(send=AsyncMock())


def test_who_may_decide():
    assert ta.can_decide(member(manage_guild=True)) and ta.can_decide(member(administrator=True))
    assert ta.can_decide(member(uid=99, owner_id=99))
    assert not ta.can_decide(member())


def test_accept_records_once_and_second_admin_sees_the_first(monkeypatch):
    conn = use(monkeypatch, Conn())
    newly, row = run(ta.accept(5, None, 10))
    assert newly and row["accepted_by"] == 10 and row["terms_version"] == ta.TERMS_VERSION
    newly, row = run(ta.accept(5, None, 11))
    assert not newly and row["accepted_by"] == 10


def test_accept_button_for_an_admin_records_and_closes_the_wizard(monkeypatch):
    use(monkeypatch, Conn())
    i = FI(member(manage_guild=True))
    run(v.TermsAcceptButton(5).callback(i))
    kw = i.response.edit_message.await_args.kwargs
    assert "accepted" in kw["embed"].title and all(c.style == discord.ButtonStyle.link for c in kw["view"].children)
    i.followup.send.assert_not_awaited()


def test_accept_button_for_a_normal_member_is_refused(monkeypatch):
    conn = use(monkeypatch, Conn())
    i = FI(member())
    run(v.TermsAcceptButton(5).callback(i))
    assert "Manage Server" in i.response.send_message.await_args.args[0] and conn.row is None
    i.response.edit_message.assert_not_awaited()


def test_buttons_refuse_another_servers_message(monkeypatch):
    i = FI(member(manage_guild=True), guild_id=6)
    run(v.TermsAcceptButton(5).callback(i))
    run(v.TermsDeclineButton(5).callback(i))
    assert i.response.send_message.await_count == 2 and "isn't for this server" in i.response.send_message.await_args.args[0]


def test_decline_asks_for_confirmation_and_only_leaves_on_yes():
    i = FI(member(manage_guild=True))
    run(v.TermsDeclineButton(5).callback(i))
    kw = i.response.send_message.await_args.kwargs
    assert kw["ephemeral"] is True and isinstance(kw["view"], v._ConfirmLeave)
    i.guild.leave.assert_not_awaited()                                   # tapping Decline alone never removes the bot
    ci = FI(member(manage_guild=True))
    run(kw["view"].leave.callback(ci))
    i.guild.leave.assert_awaited_once()


def test_decline_by_a_normal_member_is_refused():
    i = FI(member())
    run(v.TermsDeclineButton(5).callback(i))
    assert "Manage Server" in i.response.send_message.await_args.args[0] and not i.guild.leave.await_count


def test_wizard_message_has_links_to_the_site_and_is_persistent():
    e = ta.build_embed("Cat Club")
    assert ta.terms_url() in e.description and ta.privacy_url() in e.description and "Manage Server" in e.description
    view = v.build_view(5)
    assert view.timeout is None
    ids = [getattr(c, "custom_id", None) for c in view.children]
    assert "terms_accept:5" in ids and "terms_decline:5" in ids
    assert {c.url for c in view.children if getattr(c, "url", None)} == {ta.terms_url(), ta.privacy_url()}
    assert len(v.DYNAMIC_ITEMS) == 2


def _guild(channels, system=None):
    me = object()
    ch = lambda ok: SimpleNamespace(permissions_for=lambda m: perms(send_messages=ok), send=AsyncMock())
    return SimpleNamespace(id=5, name="G", me=me, system_channel=system, text_channels=channels), ch


def test_channel_pick_prefers_system_channel_then_first_postable():
    g, ch = _guild([])
    a, b, c = ch(False), ch(True), ch(True)
    g.system_channel, g.text_channels = a, [b, c]
    assert ta.pick_channel(g) is b                                     # system channel not postable -> first one that is
    g.system_channel = c
    assert ta.pick_channel(g) is c
    g.system_channel, g.text_channels = None, [a]
    assert ta.pick_channel(g) is None


def test_post_on_join_posts_once_and_skips_already_accepted(monkeypatch):
    g, ch = _guild([])
    target = ch(True)
    g.text_channels = [target]
    monkeypatch.setattr(ta, "get_acceptance", AsyncMock(return_value=None))
    assert run(ta.post_on_join(SimpleNamespace(clone_id=None), g)) is True
    kw = target.send.await_args.kwargs
    assert kw["embed"].title.startswith("📜") and kw["view"].timeout is None
    monkeypatch.setattr(ta, "get_acceptance", AsyncMock(return_value={"accepted_by": 1}))
    target.send.reset_mock()
    assert run(ta.post_on_join(SimpleNamespace(clone_id=None), g)) is False and not target.send.await_count


def test_post_on_join_never_raises(monkeypatch):
    g, ch = _guild([])
    target = ch(True)
    target.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="no"), "no"))
    g.text_channels = [target]
    monkeypatch.setattr(ta, "get_acceptance", AsyncMock(return_value=None))
    assert run(ta.post_on_join(SimpleNamespace(clone_id=None), g)) is False
    monkeypatch.setattr(ta, "get_acceptance", AsyncMock(side_effect=RuntimeError("db")))
    assert run(ta.post_on_join(SimpleNamespace(clone_id=None), g)) is False


def test_join_flow_posts_the_wizard_before_the_welcome_wizard_and_registers_the_buttons():
    from pathlib import Path
    src = Path(__file__).resolve().parents[2].joinpath("discord_bot/bot.py").read_text()
    assert src.index("terms_accept.post_on_join") < src.index("post_setup_wizard_on_join(guild)")
    assert "TERMS_DYNAMIC_ITEMS" in src and src.count("TERMS_DYNAMIC_ITEMS") == 2
