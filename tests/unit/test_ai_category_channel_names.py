"""A category and a channel with the same name (category "gallo" + text channel "gallo") must not make the AI refuse or guess."""
from types import SimpleNamespace as N

import discord

from discord_bot.cogs import ai_tools as at
from discord_bot.cogs import _ai_bulk_ops as ops
from modules import ai_names

T, V, CAT = discord.ChannelType.text, discord.ChannelType.voice, discord.ChannelType.category


def ch(i, name, t=T): return N(id=i, name=name, type=t)


CHANNELS = [ch(1, "gallo", CAT), ch(2, "gallo", T), ch(3, "general")]


def test_hint_parsing():
    assert ai_names.split_kind_hint("category gallo") == ("gallo", "category")
    assert ai_names.split_kind_hint("the gallo category") == ("gallo", "category")
    assert ai_names.split_kind_hint("Category: gallo") == ("gallo", "category")
    assert ai_names.split_kind_hint("gallo (category)") == ("gallo", "category")
    assert ai_names.split_kind_hint("#gallo") == ("gallo", "channel")
    assert ai_names.split_kind_hint("gallo channel") == ("gallo", "channel")
    assert ai_names.split_kind_hint("gallo") == ("gallo", None)
    assert ai_names.split_kind_hint("category") == ("category", None)      # just the word, not a hint


def test_plain_name_means_the_channel_not_a_tie():
    got, _ = at.find_channel_by_name(CHANNELS, "gallo")
    assert got.id == 2
    got, _ = at.find_channel_by_name(CHANNELS, "#gallo")
    assert got.id == 2


def test_category_wording_picks_the_category():
    assert at.find_channel_by_name(CHANNELS, "category gallo")[0].id == 1
    assert at.find_channel_by_name(CHANNELS, "gallo category")[0].id == 1
    assert at.find_channel_by_name(CHANNELS, "gallo", prefer="category")[0].id == 1


def test_two_channels_with_one_name_still_ask():
    got, cands = at.find_channel_by_name(CHANNELS + [ch(4, "gallo", V)], "gallo")
    assert got is None and {c.id for c in cands} == {2, 4}                  # text vs voice: a real tie, ask


def test_only_a_category_exists():
    got, _ = at.find_channel_by_name([ch(1, "gallo", CAT)], "gallo")
    assert got.id == 1


def test_unresolved_message_labels_the_kind():
    msg = at.unresolved_message("channel", "#gallo", [CHANNELS[0], CHANNELS[1]])
    assert "(category)" in msg and "(channel)" in msg


def test_narrow_never_empties_a_list():
    assert ai_names.narrow([CHANNELS[0]], "channel") == [CHANNELS[0]]
    assert ai_names.narrow([], "category") == []
