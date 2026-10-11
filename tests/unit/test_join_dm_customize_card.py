"""Join DM welcome sub-screen: a 'Customize Card' button sits beside Card options / Preview / Back, opens the ultra-card
wizard, and is only usable by someone with Manage Server in that server."""
import asyncio
import re
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_join_dm as jd


def _sub_view():
    return jd.build_welcome_sub_view(123, None, discord.ui.Container(discord.ui.TextDisplay("x")), "channel")


def _labels(view):
    out = []
    for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
        out.append([getattr(getattr(c, "item", c), "label", None) for c in row.children])
    return out


def test_sub_screen_shows_customize_card_and_keeps_every_other_button():
    rows = _labels(_sub_view())
    assert rows[0] == ["Edit message", "Change channel", "Switch to DM delivery"]
    assert rows[1] == ["Card options", "Customize Card", "Preview", "Back"]
    assert all(len(r) <= 5 for r in rows)


def test_button_is_registered_and_its_custom_id_roundtrips():
    assert jd._WelcomeCustomizeCardButton in jd.DYNAMIC_ITEMS
    for clone in (None, 7):
        b = jd._WelcomeCustomizeCardButton(123, clone)
        cid = b.item.custom_id
        m = re.match(jd._WelcomeCustomizeCardButton.__discord_ui_compiled_template__.pattern, cid)
        assert m and m.group(1) == "123" and m.group(2) == ("-" if clone is None else "7")
        back = asyncio.run(jd._WelcomeCustomizeCardButton.from_custom_id(None, None, m))
        assert (back.guild_id, back.clone_id) == (123, clone)


def test_custom_id_does_not_collide_with_a_sibling_template():
    cid = jd._WelcomeCustomizeCardButton(123, None).item.custom_id
    for other in (jd._WelcomeCardOptionsButton, jd._WelcomePreviewRefreshButton, jd._WelcomeBackButton, jd._WelcomeEditButton):
        assert not re.match(other.__discord_ui_compiled_template__.pattern, cid)


class _Resp:
    def __init__(self):
        self.deferred = None

    def is_done(self):
        return self.deferred is not None

    async def defer(self, ephemeral=False, **kw):
        self.deferred = ephemeral


def _interaction():
    return SimpleNamespace(response=_Resp(), guild=None, user=SimpleNamespace(id=5))


def test_denied_without_manage_server_and_nothing_opens(monkeypatch):
    opened = []

    async def deny(interaction, invoker_id, guild_id=None):
        assert invoker_id is None and guild_id == 123      # None => permission is what decides, not "who ran the command"
        return False

    async def open_(*a, **k):
        opened.append(a)
    import discord_bot.cogs._views_welcome as vw
    import discord_bot.cogs._views_card_customize as cc
    monkeypatch.setattr(vw, "_check_access", deny)
    monkeypatch.setattr(cc, "open_customize_wizard", open_)
    it = _interaction()
    asyncio.run(jd._WelcomeCustomizeCardButton(123, None).callback(it))
    assert opened == [] and it.response.deferred is None


def test_allowed_manager_gets_the_wizard_for_that_server_and_clone(monkeypatch):
    opened = []

    async def allow(interaction, invoker_id, guild_id=None):
        return True

    async def open_(interaction, guild_id, clone_id):
        opened.append((guild_id, clone_id))
    import discord_bot.cogs._views_welcome as vw
    import discord_bot.cogs._views_card_customize as cc
    monkeypatch.setattr(vw, "_check_access", allow)
    monkeypatch.setattr(cc, "open_customize_wizard", open_)
    it = _interaction()
    asyncio.run(jd._WelcomeCustomizeCardButton(123, 7).callback(it))
    assert opened == [(123, 7)] and it.response.deferred is False      # DM: not ephemeral, same as the Card options button
