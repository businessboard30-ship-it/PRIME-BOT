"""/birthday is Components V2, never times out, and every click works after a restart."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import birthdays as bc
from modules import channel_finder as cf


def _walk(view):
    out = []
    def go(item):
        out.append(item)
        for child in getattr(item, "children", []) or []:
            go(child)
        if getattr(item, "accessory", None) is not None:      # Section's Thumbnail/Button
            go(item.accessory)
    for it in view.children:
        go(it)
    return out


def _ids(view):
    return [getattr(getattr(i, "item", i), "custom_id", None) for i in _walk(view)
            if getattr(getattr(i, "item", i), "custom_id", None)]


def _text(view):
    return "\n".join(i.content for i in _walk(view) if isinstance(i, discord.ui.TextDisplay))


def _guild(cfg_channel=True):
    ch = MagicMock(); ch.id = 77; ch.mention = "<#77>"
    g = MagicMock(); g.id = 5; g.name = "Srv"
    g.get_channel = MagicMock(return_value=ch if cfg_channel else None)
    g.get_role = MagicMock(return_value=None)
    return g


CFG = {"enabled": True, "channel_id": 77, "ping_role_id": None, "announce_hour": 9,
       "utc_offset_hours": 0, "message": None, "birthday_role_id": None, "extra_messages": []}


def test_every_panel_is_v2_without_timeout_and_serialises():
    for v in (bc.build_main_view(True), bc.build_main_view(False), bc._month_view(),
              bc.build_setup_view(_guild(), CFG, False), bc.build_setup_view(_guild(), CFG, True)):
        assert isinstance(v, discord.ui.LayoutView) and v.timeout is None
        assert v.to_components()


def test_non_admin_never_sees_server_setup_button():
    assert "bday:setup" in _ids(bc.build_main_view(True))
    assert "bday:setup" not in _ids(bc.build_main_view(False))


def test_every_rendered_custom_id_routes_to_a_registered_item():
    """After a restart discord.py matches custom_ids against the registered templates."""
    # modules.channel_finder (installed at bot start-up) adds a "Find by name" button under every channel
    # dropdown; it is routed by its own registered item, so count those templates as owners too.
    templates = [c.__discord_ui_compiled_template__ for c in bc.DYNAMIC_ITEMS + tuple(cf.DYNAMIC_ITEMS)]
    ids = set(_ids(bc.build_main_view(True)) + _ids(bc._month_view()) + _ids(bc.build_setup_view(_guild(), CFG, True)))
    assert len(ids) >= 14
    for cid in ids:
        assert sum(1 for t in templates if t.match(cid)) == 1, cid      # exactly one owner each
    assert any(i.startswith("bday:") for i in ids)
    for cls in bc.DYNAMIC_ITEMS:                                       # rebuilt with no state at all
        m = cls.__discord_ui_compiled_template__.match(cls().item.custom_id)
        assert isinstance(asyncio.run(cls.from_custom_id(None, None, m)), cls)


def _inter(perm=True, guild=None):
    g = guild or _guild()
    user = SimpleNamespace(id=9, guild_permissions=SimpleNamespace(manage_guild=perm))
    cog = SimpleNamespace(wake=MagicMock(), _upcoming_cooldown={})
    client = SimpleNamespace(clone_id=None, get_cog=lambda n: cog)
    resp = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                           defer=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(guild=g, user=user, client=client, response=resp,
                           followup=SimpleNamespace(send=AsyncMock())), cog


def test_toggle_uses_live_db_value_not_the_stale_label(monkeypatch):
    saved = {}
    monkeypatch.setattr(bc.store, "get_config", AsyncMock(return_value={**CFG, "enabled": True}))
    monkeypatch.setattr(bc.store, "set_config", AsyncMock(side_effect=lambda *a, **k: saved.update(k) or {**CFG, **k}))
    monkeypatch.setattr(bc, "_is_premium", AsyncMock(return_value=False))
    inter, cog = _inter()
    asyncio.run(bc.ToggleButton().callback(inter))            # fresh item: default label says "Turn on"
    assert saved == {"enabled": False}                        # but the DB said it was on, so it turns off
    assert inter.response.edit_message.await_args.kwargs["view"].timeout is None


def test_admin_controls_reject_members_without_manage_server(monkeypatch):
    set_config = AsyncMock()
    monkeypatch.setattr(bc.store, "set_config", set_config)
    inter, _ = _inter(perm=False)
    for item in (bc.ToggleButton(), bc.EditMessageButton(), bc.SendTestButton(), bc.ServerSetupButton()):
        asyncio.run(item.callback(inter))
    set_config.assert_not_awaited()
    assert inter.response.send_message.await_count == 4
    assert "Manage Server" in inter.response.send_message.await_args.args[0]


def test_server_setup_opens_a_fresh_panel_from_db(monkeypatch):
    monkeypatch.setattr(bc.store, "get_config", AsyncMock(return_value=CFG))
    monkeypatch.setattr(bc, "_is_premium", AsyncMock(return_value=False))
    inter, _ = _inter()
    asyncio.run(bc.ServerSetupButton().callback(inter))
    view = inter.response.send_message.await_args.kwargs["view"]
    assert "Birthday setup" in _text(view) and inter.response.send_message.await_args.kwargs["ephemeral"] is True


def _member(uid=1):
    m = MagicMock(); m.id = uid; m.mention = f"<@{uid}>"; m.display_name = f"User{uid}"
    m.display_avatar.url = "https://cdn.example/a.png"
    return m


def test_plain_announcement_is_v2_and_pings_the_birthday_person():
    view, allowed = bc._build_post(_guild(), CFG, False, [_member(1)])
    assert isinstance(view, discord.ui.LayoutView) and view.to_components()
    assert "<@1>" in _text(view)
    assert allowed.users and allowed.everyone is False


def test_styled_announcement_has_pings_above_card_avatar_banner_and_footer():
    cfg = {**CFG, "embed_title": "Party!", "embed_color": 0x112233, "embed_image_url": "https://x.com/a.png",
           "embed_footer": "cake time", "show_avatar": True}
    view, allowed = bc._build_post(_guild(), cfg, True, [_member(1)])
    assert view.to_components()
    kinds = [type(i).__name__ for i in view.children]
    assert kinds[0] == "TextDisplay" and "Container" in kinds                # ping line, then the card
    assert "<@1>" in view.children[0].content
    inner = [type(i).__name__ for i in _walk(view)]
    assert {"Section", "Thumbnail", "MediaGallery"} <= set(inner)
    assert "cake time" in _text(view) and "Party!" in _text(view)


def test_test_message_never_pings_anyone():
    view, allowed = bc._build_post(_guild(), CFG, False, [_member(1)], test=True)
    assert allowed.users == [] and "Test message" in _text(view)


def test_no_classic_views_or_embeds_left_in_birthdays():
    import inspect
    src = inspect.getsource(bc)
    assert "discord.ui.View)" not in src and "discord.Embed(" not in src


def test_setup_panel_stays_valid_with_the_channel_finder_installed():
    """The finder inserts a 'Find by name' row under the channel dropdown; the panel must still be
    a valid V2 message inside Discord's 40-component limit, with the real dropdown routed by chfind:."""
    cf.install()
    v = bc.build_setup_view(_guild(), CFG, True)
    assert v.to_components() and len(list(v.walk_children())) <= 40
    ids = _ids(v)
    assert "bday:channel" in ids and f"chfind:bday:channel" in ids
    assert any(t.match("chfind:bday:channel") for t in [c.__discord_ui_compiled_template__ for c in cf.DYNAMIC_ITEMS])
