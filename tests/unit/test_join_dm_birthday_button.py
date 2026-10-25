"""Birthdays on the join DM's 'More features' screen."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import birthdays as bc

run = asyncio.run


def _kids(view):
    return list(view.walk_children())


def test_more_features_lists_birthdays_with_a_routable_persistent_button():
    for clone in (None, 7):
        view = jd.build_join_dm_more_view(123, clone)
        kids = _kids(view)
        assert len(kids) <= 40 and view.timeout is None and view.to_components()
        text = "\n".join(k.content for k in kids if isinstance(k, discord.ui.TextDisplay))
        assert "Birthdays" in text
        cid = f"join_dm_birthday:123:{'-' if clone is None else clone}"
        ids = [getattr(k, "custom_id", "") for k in kids]
        assert cid in ids and all(len(i) <= 100 for i in ids if i)
    assert jd._BirthdayButton in jd.DYNAMIC_ITEMS
    m = jd._BirthdayButton.__discord_ui_compiled_template__.match("join_dm_birthday:123:7")
    item = run(jd._BirthdayButton.from_custom_id(None, None, m))
    assert (item.guild_id, item.clone_id) == (123, 7)           # rebuilt after a restart with no state


def _inter(guild):
    i = MagicMock()
    i.guild = None                                              # the join DM: no server context
    i.user = SimpleNamespace(id=9)
    i.client = SimpleNamespace(get_guild=lambda gid: guild, clone_id=None)
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _guild(channel):
    g = MagicMock(); g.id = 123; g.name = "Srv"; g.system_channel = channel
    g.text_channels = [channel] if channel else []
    g.get_channel = MagicMock(return_value=channel); g.get_role = MagicMock(return_value=None)
    return g


def _channel():
    ch = MagicMock(); ch.id = 5; ch.mention = "<#5>"; ch.send = AsyncMock()
    ch.permissions_for = MagicMock(return_value=SimpleNamespace(send_messages=True))
    return ch


def _wire(monkeypatch, allowed=True):
    import discord_bot.cogs._views_bot_profile as bp
    monkeypatch.setattr(bp, "_check_access", AsyncMock(return_value=allowed))
    monkeypatch.setattr(bc, "_is_premium", AsyncMock(return_value=False))
    from modules import birthday_store
    monkeypatch.setattr(birthday_store, "get_config", AsyncMock(return_value={
        "enabled": False, "channel_id": None, "ping_role_id": None, "announce_hour": 9, "utc_offset_hours": 0,
        "message": None, "birthday_role_id": None, "extra_messages": []}))


def test_without_manage_server_nothing_is_posted(monkeypatch):
    _wire(monkeypatch, allowed=False)
    ch = _channel(); i = _inter(_guild(ch))
    run(jd._BirthdayButton(123, None).callback(i))
    ch.send.assert_not_awaited(); i.response.defer.assert_not_awaited()


def test_setup_panel_is_posted_in_the_default_channel_without_pinging(monkeypatch):
    _wire(monkeypatch)
    ch = _channel(); i = _inter(_guild(ch))
    run(jd._BirthdayButton(123, None).callback(i))
    kw = ch.send.await_args.kwargs
    view = kw["view"]
    assert isinstance(view, discord.ui.LayoutView) and view.timeout is None and view.to_components()
    text = "\n".join(k.content for k in view.walk_children() if isinstance(k, discord.ui.TextDisplay))
    assert "Birthday setup" in text and "Manage Server" in text
    assert kw["allowed_mentions"].everyone is False and kw["allowed_mentions"].users is False
    assert "<#5>" in i.followup.send.await_args.args[0] and i.followup.send.await_args.kwargs["ephemeral"] is True


def test_no_postable_channel_says_so(monkeypatch):
    _wire(monkeypatch)
    i = _inter(_guild(None))
    run(jd._BirthdayButton(123, None).callback(i))
    assert "couldn't find a channel" in i.followup.send.await_args.args[0]


def test_forbidden_channel_is_reported_not_raised(monkeypatch):
    _wire(monkeypatch)
    ch = _channel()
    ch.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
    i = _inter(_guild(ch))
    run(jd._BirthdayButton(123, None).callback(i))
    assert "couldn't post" in i.followup.send.await_args.args[0]
