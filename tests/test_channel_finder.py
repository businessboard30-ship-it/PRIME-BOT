import asyncio
import types

import discord

from modules import channel_finder as cf


def _ch(name, pos=0, t=discord.ChannelType.text):
    return types.SimpleNamespace(name=name, position=pos, type=t)


def test_match_ignores_fonts_and_brackets():
    chans = [_ch("📣【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】", 1), _ch("[welcome]-staff", 2), _ch("general", 3)]
    names = [c.name for c in cf.match_channels(chans, "welcome")]
    assert names == ["📣【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】", "[welcome]-staff"]  # exact first, then partial
    assert cf.match_channels(chans, "𝐠𝐞𝐧𝐞𝐫𝐚𝐥") and cf.match_channels(chans, "") == []


def test_match_respects_types():
    chans = [_ch("welcome", 1, discord.ChannelType.voice), _ch("welcome-2", 2)]
    got = cf.match_channels(chans, "welcome", types=[discord.ChannelType.text])
    assert [c.name for c in got] == ["welcome-2"]


class _Sel(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=r"^t:chan:(\d+)$"):
    def __init__(self, n):
        super().__init__(discord.ui.ChannelSelect(custom_id=f"t:chan:{n}"))


def test_install_adds_button_once_and_respects_limit():
    async def run():
        cf.install(); cf.install()
        view = discord.ui.LayoutView(timeout=None)
        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay("hi"))
        row = discord.ui.ActionRow(); row.add_item(_Sel(1)); box.add_item(row)
        view.add_item(box)
        ids = [getattr(getattr(c, "item", c), "custom_id", None)
               for r in box.children for c in getattr(r, "children", [])]
        assert ids == ["t:chan:1", "chfind:t:chan:1"]
        assert view.total_children_count <= 40
        # near the limit: no button, no crash
        view2 = discord.ui.LayoutView(timeout=None)
        box2 = discord.ui.Container()
        for _ in range(36):
            box2.add_item(discord.ui.TextDisplay("x"))
        row2 = discord.ui.ActionRow(); row2.add_item(_Sel(2)); box2.add_item(row2)
        view2.add_item(box2)
        assert view2.total_children_count <= 40
        assert not any(isinstance(c, cf.ChannelFindButton) for r in box2.children for c in getattr(r, "children", []))
    asyncio.run(run())
