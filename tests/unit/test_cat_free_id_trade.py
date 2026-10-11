"""Trade by ID in a FREE server: type an ID, and it works for people who are members of THIS server. Not global."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_adopt as ca
from modules import cat_ids as ids
from modules import cat_setup as cs
from tests.unit.test_cat_trade_ids import _cat, _interaction, _parent


def run(c):
    return asyncio.run(c)


class Box:                                   # text input stand-in
    def __init__(self, t): self.t = t
    def __str__(self): return self.t


def _modal():
    m = v.TradeIdModal(_parent())
    m.trade_id = Box("a b c d e f g")
    return m


def _member(uid=77, bot=False):
    return SimpleNamespace(id=uid, bot=bot, display_name="Neighbour", mention=f"<@{uid}>")


def _setup(monkeypatch, premium, owner_id=77, member=None, fetch_raises=False):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=premium))
    monkeypatch.setattr(ids, "find_identity", AsyncMock(return_value=None if owner_id is None else (owner_id, None)))
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[_cat(5, owner_id)]))
    i = _interaction()
    if fetch_raises:
        i.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "unknown member"))
    else:
        i.guild.fetch_member = AsyncMock(return_value=member)
    i.guild.get_member = lambda uid: None          # not cached: forces the fetch path too
    return i


def _view(i):
    return i.response.edit_message.call_args.kwargs["view"]


def test_free_server_member_id_opens_a_local_trade(monkeypatch):
    i = _setup(monkeypatch, premium=False, member=_member())
    run(_modal().on_submit(i))
    view = _view(i)
    assert isinstance(view, v.TradeAskView) and view.remote is False and view.target.id == 77
    assert "this server" in i.response.edit_message.call_args.kwargs["content"]
    i.response.send_message.assert_not_called()


def test_free_server_cached_member_needs_no_api_lookup(monkeypatch):
    i = _setup(monkeypatch, premium=False, member=None)
    i.guild.get_member = lambda uid: _member(uid)
    run(_modal().on_submit(i))
    i.guild.fetch_member.assert_not_called()
    assert isinstance(_view(i), v.TradeAskView)


def test_free_server_id_of_someone_in_another_server_is_refused(monkeypatch):
    i = _setup(monkeypatch, premium=False, fetch_raises=True)
    run(_modal().on_submit(i))
    msg = i.response.send_message.call_args.args[0]
    assert "this server" in msg and "Premium" in msg
    i.response.edit_message.assert_not_called()
    i.client.fetch_user.assert_not_called()             # never reaches out to a person from another server


def test_free_server_unknown_id_and_outside_id_get_the_same_answer(monkeypatch):
    """So the box can't be used to find out whether an ID exists."""
    i1 = _setup(monkeypatch, premium=False, fetch_raises=True)
    run(_modal().on_submit(i1))
    i2 = _setup(monkeypatch, premium=False, owner_id=None)
    run(_modal().on_submit(i2))
    assert i1.response.send_message.call_args.args[0] == i2.response.send_message.call_args.args[0]


def test_free_server_cannot_dm_across_servers_by_any_route(monkeypatch):
    i = _setup(monkeypatch, premium=False, fetch_raises=True)
    run(_modal().on_submit(i))
    i.client.fetch_user.assert_not_called()
    assert not i.channel.send.called


def test_own_id_is_still_refused_in_a_free_server(monkeypatch):
    i = _setup(monkeypatch, premium=False, owner_id=10, member=_member(10))     # find_owner == the person typing (id 10)
    run(_modal().on_submit(i))
    assert "your own" in i.response.send_message.call_args.args[0]


def test_a_bot_member_is_refused(monkeypatch):
    i = _setup(monkeypatch, premium=False, member=_member(bot=True))
    run(_modal().on_submit(i))
    assert "doesn't belong to a person" in i.response.send_message.call_args.args[0]
    i.response.edit_message.assert_not_called()


def test_premium_member_of_this_server_also_goes_local(monkeypatch):
    i = _setup(monkeypatch, premium=True, member=_member())
    run(_modal().on_submit(i))
    assert _view(i).remote is False
    i.client.fetch_user.assert_not_called()


def test_premium_outside_person_still_goes_across_servers(monkeypatch):
    i = _setup(monkeypatch, premium=True, fetch_raises=True)
    i.client.fetch_user = AsyncMock(return_value=SimpleNamespace(id=77, bot=False, display_name="Far", mention="<@77>"))
    run(_modal().on_submit(i))
    assert _view(i).remote is True


def test_premium_unknown_id_keeps_the_original_message(monkeypatch):
    i = _setup(monkeypatch, premium=True, owner_id=None)
    run(_modal().on_submit(i))
    assert "don't know that ID" in i.response.send_message.call_args.args[0]


def test_remote_offer_is_still_premium_only_at_send_time(monkeypatch):
    """Downgrade between opening the box and sending: the final guard still blocks a cross-server DM."""
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=False))
    v._remote_at.clear(); v._remote_pending.clear()
    target = SimpleNamespace(id=99, display_name="Far", mention="<@99>", send=AsyncMock())
    view = v.TradeAskView(_parent(), target, [], remote=True)
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    run(view._chosen(_interaction()))
    target.send.assert_not_called()


def test_button_label_no_longer_says_premium_and_the_card_explains_both(monkeypatch):
    view = v.TradeTargetView(_parent())
    labels = [getattr(c, "label", None) for c in view.children]
    assert "Trade by ID" in labels and not any(l and "Premium" in l for l in labels)
    import inspect
    src = inspect.getsource(v.CollectionView.embed)
    assert "Members of your server" in src and "Premium" in src
