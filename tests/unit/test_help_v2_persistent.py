"""/help is a Components V2 message that never times out and survives a restart."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_help as vh
from discord_bot.cogs.help import CATEGORIES


def _walk(view):
    out = []
    def go(item):
        out.append(item)
        for child in getattr(item, "children", []) or []:
            go(child)
    for it in view.children:
        go(it)
    return out


def _custom_ids(view):
    ids = []
    for it in _walk(view):
        inner = getattr(it, "item", it)
        cid = getattr(inner, "custom_id", None)
        if cid:
            ids.append(cid)
    return ids


def _text(view):
    return "\n".join(i.content for i in _walk(view) if isinstance(i, discord.ui.TextDisplay))


def _interaction(**over):
    client = SimpleNamespace(user=SimpleNamespace(display_name="PrimeBot"), clone_id=None)
    i = SimpleNamespace(client=client, guild_id=123, response=SimpleNamespace(
        edit_message=AsyncMock(), send_modal=AsyncMock()))
    i.__dict__.update(over)
    return i


def test_views_never_time_out_and_are_v2():
    for v in (vh.build_home_view("PrimeBot", 1, None),
              vh.build_category_view(next(iter(CATEGORIES)), 1, None),
              vh.build_results_view("x", [], 1, None)):
        assert isinstance(v, discord.ui.LayoutView)
        assert v.timeout is None
        assert v.to_components()                      # serialises (valid V2 payload)


def test_every_interactive_custom_id_is_stable_and_routable():
    """After a restart discord.py routes by custom_id regex to the registered items."""
    ids = _custom_ids(vh.build_home_view("PrimeBot", 1, None))
    assert {"help:cat", "help:search", "help:mydata", "premium_pitch"} <= set(ids)
    templates = [c.__discord_ui_compiled_template__ for c in vh.HELP_DYNAMIC_ITEMS]
    for cid in ("help:cat", "help:search", "help:mydata"):
        assert any(t.match(cid) for t in templates), cid
    # the fresh item built from a custom_id needs no per-message state
    for cls in vh.HELP_DYNAMIC_ITEMS:
        m = cls.__discord_ui_compiled_template__.match(cls().item.custom_id)
        built = asyncio.run(cls.from_custom_id(None, None, m))
        assert isinstance(built, cls)


def test_every_category_fits_v2_limits_and_select_has_all_options():
    for name in CATEGORIES:
        assert len(_text(vh.build_category_view(name, 1, None))) < 4000
    sel = next(i for i in _walk(vh.build_home_view("B", 1, None)) if isinstance(i, vh.HelpCategorySelect))
    assert [o.value for o in sel.item.options] == list(CATEGORIES)
    assert len(sel.item.options) <= 25


def test_select_callback_rerenders_chosen_category_without_state():
    name = list(CATEGORIES)[1]
    item = vh.HelpCategorySelect()
    item.item._values = [name]                         # what discord fills in on a real click
    inter = _interaction()
    asyncio.run(item.callback(inter))
    view = inter.response.edit_message.await_args.kwargs["view"]
    assert name in _text(view) and view.timeout is None
    chosen = next(i for i in _walk(view) if isinstance(i, vh.HelpCategorySelect))
    assert [o.value for o in chosen.item.options if o.default] == [name]


def test_stale_category_falls_back_to_home():
    item = vh.HelpCategorySelect()
    item.item._values = ["🗑️ Removed Category"]
    inter = _interaction()
    asyncio.run(item.callback(inter))
    assert "Help" in _text(inter.response.edit_message.await_args.kwargs["view"])


def test_search_modal_single_hit_jumps_and_no_hit_says_so():
    modal = vh.SearchModal()
    modal.query._value = "voicexp"
    inter = _interaction()
    asyncio.run(modal.on_submit(inter))
    text = _text(inter.response.edit_message.await_args.kwargs["view"])
    assert "Jumped here" in text and "**" in text
    modal2 = vh.SearchModal(); modal2.query._value = "zzzzqqqq"
    inter2 = _interaction()
    asyncio.run(modal2.on_submit(inter2))
    assert "No matching commands" in _text(inter2.response.edit_message.await_args.kwargs["view"])


def test_search_button_opens_modal():
    inter = _interaction()
    asyncio.run(vh.HelpSearchButton().callback(inter))
    assert isinstance(inter.response.send_modal.await_args.args[0], vh.SearchModal)
