"""The AI must never act on a channel/role it guessed. A named channel that can't be matched to exactly one real one stops the
command with a question; it must NOT fall back to the channel the person is typing in (that locked #mod-logs instead of #general)."""
from types import SimpleNamespace as N

import discord
import pytest

from discord_bot.cogs import ai_tools as at

T = discord.ChannelType.text
CAT = discord.ChannelType.category


def ch(i, name, t=T): return N(id=i, name=name, type=t, mention=f"<#{i}>")


CHANNELS = [ch(1, "《mod-logs》1"), ch(2, "💬・general-chat"), ch(3, "《general》"), ch(4, "📣【announcements】"),
            ch(5, "welcome"), ch(6, "welcome-guide"), ch(7, "GENERAL INFO", CAT)]


def test_decorated_names_match_by_their_plain_name():
    got, _ = at.find_channel_by_name(CHANNELS, "#general")
    assert got.id == 3                                          # 《general》, exact plain match beats general-chat
    got, _ = at.find_channel_by_name(CHANNELS, "announcements")
    assert got.id == 4


def test_one_contains_match_is_accepted_but_ambiguity_is_not():
    only = [c for c in CHANNELS if c.id not in (3,)]
    got, cands = at.find_channel_by_name(only, "general")       # only 💬・general-chat contains it (category is not nameable)
    assert got.id == 2
    got, cands = at.find_channel_by_name(CHANNELS + [ch(8, "general-2")], "gener")
    assert got is None and {c.id for c in cands} == {2, 3, 8}   # several contain "gener": ask, don't pick


def test_unknown_name_matches_nothing():
    got, cands = at.find_channel_by_name(CHANNELS, "does-not-exist")
    assert got is None and cands == []


def test_message_lists_candidates_as_mentions():
    msg = at.unresolved_message("channel", "#general", [CHANNELS[1], CHANNELS[2]])
    assert "didn't run that" in msg and "<#2>" in msg and "<#3>" in msg
    assert "didn't run that" in at.unresolved_message("channel", "#x", [])


class Cmd:
    pass


def cog_with(channels, roles=()):
    cog = at.AIToolsCog.__new__(at.AIToolsCog)
    cog._param_defs = lambda command: [{"name": "channel", "type": "channel"}, {"name": "role", "type": "role"}]
    guild = N(channels=channels, get_channel=lambda i: next((c for c in channels if c.id == i), None),
              roles=list(roles), get_role=lambda i: next((r for r in roles if r.id == i), None), default_role=N(id=0, name="@everyone"),
              members=[], get_member=lambda i: None)
    inter = N(guild=guild, user=N(id=1), message=None, channel=ch(1, "《mod-logs》1"))
    return cog, inter


def test_named_channel_resolves_to_the_right_one_not_the_current_channel():
    cog, inter = cog_with(CHANNELS)
    assert cog._resolve_tool_args(inter, Cmd(), {"channel": "#general"})["channel"].id == 3


def test_unresolvable_channel_raises_instead_of_dropping():
    cog, inter = cog_with(CHANNELS)
    with pytest.raises(at.ArgUnresolved):
        cog._resolve_tool_args(inter, Cmd(), {"channel": "#raid-room"})
    with pytest.raises(at.ArgUnresolved):                       # a mention/id of a channel that doesn't exist
        cog._resolve_tool_args(inter, Cmd(), {"channel": "<#999999>"})


def test_this_and_here_still_mean_the_current_channel():
    cog, inter = cog_with(CHANNELS)
    assert cog._resolve_tool_args(inter, Cmd(), {"channel": "this"})["channel"].id == 1
    assert cog._resolve_tool_args(inter, Cmd(), {"channel": "here"})["channel"].id == 1


def test_role_unresolved_raises_and_everyone_maps_to_the_default_role():
    roles = [N(id=10, name="Mods"), N(id=11, name="mods")]
    cog, inter = cog_with(CHANNELS, roles)
    with pytest.raises(at.ArgUnresolved):
        cog._resolve_tool_args(inter, Cmd(), {"role": "Admins"})
    with pytest.raises(at.ArgUnresolved):                       # two roles differ only by case: ambiguous
        cog._resolve_tool_args(inter, Cmd(), {"role": "mods"})
    assert cog._resolve_tool_args(inter, Cmd(), {"role": "@everyone"})["role"].id == 0


def test_both_call_sites_handle_the_refusal():
    src = open("discord_bot/cogs/ai_tools.py", encoding="utf-8").read()
    assert src.count("except ArgUnresolved as exc:") == 2
