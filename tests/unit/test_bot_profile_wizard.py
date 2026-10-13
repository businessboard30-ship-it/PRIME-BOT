"""Join DM > More features > Bot profile wizard (rename / avatar / banner / bio, free)."""
import asyncio
import io

import discord

from discord_bot.cogs import _views_bot_profile as bp
from discord_bot.cogs import _views_join_dm as jd


def run(coro):
    return asyncio.run(coro)


def _ids(view):
    return [getattr(c, "custom_id", None) for c in view.walk_children() if getattr(c, "custom_id", None)]


def test_more_screen_has_bot_profile_button_and_it_is_registered():
    assert jd._BotProfileButton in jd.DYNAMIC_ITEMS
    for item in bp.DYNAMIC_ITEMS:
        assert item in jd.DYNAMIC_ITEMS
    assert "join_dm_botprof:123:-" in _ids(jd.build_join_dm_more_view(123, None))
    assert "join_dm_botprof:123:7" in _ids(jd.build_join_dm_more_view(123, 7))


def test_dynamic_item_templates_do_not_collide():
    pats = [i.__discord_ui_compiled_template__.pattern for i in jd.DYNAMIC_ITEMS]
    assert len(pats) == len(set(pats))


def test_panel_buttons_and_owner_only_global_button():
    plain = _ids(bp.build_profile_view(None, 5, None, owner=False))
    owner = _ids(bp.build_profile_view(None, 5, 9, owner=True))
    for a in ("name", "avatar", "banner", "bio", "reset", "done"):
        assert f"botprof_{a}:5:-" in plain
    assert "botprof_global:5:-" not in plain
    assert "botprof_global:5:9" in owner


def test_prepare_image_square_avatar_and_wide_banner():
    from PIL import Image
    import base64
    buf = io.BytesIO()
    Image.new("RGB", (300, 120), "red").save(buf, "PNG")
    for kind, size in (("avatar", (bp.AVATAR_SIZE,) * 2), ("banner", bp.BANNER_SIZE)):
        uri = bp._prepare_image(buf.getvalue(), kind)
        assert uri.startswith("data:image/png;base64,")
        out = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
        assert out.size == size
    assert bp._prepare_image(b"not an image", "avatar") is None


def test_server_profile_patch_uses_member_me_route_and_reports_errors():
    class Http:
        def __init__(self, exc=None):
            self.calls, self.exc = [], exc

        async def request(self, route, **kw):
            self.calls.append((route.method, route.path, kw.get("json")))
            if self.exc:
                raise self.exc

    class Client:
        def __init__(self, http):
            self.http = http

    ok = Http()
    assert run(bp._patch_server_profile(Client(ok), 42, {"nick": "Prime"})) is None
    assert ok.calls == [("PATCH", "/guilds/{guild_id}/members/@me", {"nick": "Prime"})]

    class Resp:
        status, reason = 403, "Forbidden"

    bad = Http(discord.Forbidden(Resp(), "Missing Permissions"))
    assert "permission" in run(bp._patch_server_profile(Client(bad), 42, {"nick": "x"}))


def test_clone_owner_check(monkeypatch):
    async def fake(clone_id):
        return {"owner_id": 77} if clone_id == 1 else None
    monkeypatch.setattr(bp.db, "get_discord_clone", fake, raising=False)
    assert run(bp.is_clone_owner(1, 77)) is True
    assert run(bp.is_clone_owner(1, 78)) is False
    assert run(bp.is_clone_owner(2, 77)) is False
    assert run(bp.is_clone_owner(None, 77)) is False


def test_server_panel_home_has_edit_bot_profile_and_keeps_every_other_button():
    from types import SimpleNamespace
    from discord_bot.cogs import _views_server_panel as sp
    v = sp.HomeView.__new__(sp.HomeView)
    v.inspect = None
    v.nav = v.nav_p2 = v.nav_p3 = v.nav_p4 = v.nav_p6 = lambda *a: (lambda i: None)
    labels = [b.label for b in sp.HomeView.controls(v)]
    assert "Edit bot profile" in labels
    for keep in ("Setup", "Welcome & verification", "Moderation", "Community", "Tickets", "Channels & logs", "Tools",
                 "More features", "Stats", "Change history", "Help & tools", "Premium", "Quick enable"):
        assert keep in labels


def test_botprofile_is_one_command_that_opens_the_wizard():
    from discord_bot.cogs import botprofile as bp
    cmd = bp.BotProfileCog.botprofile
    assert cmd.name == "botprofile" and not hasattr(bp.BotProfileCog, "group")
def test_join_dm_page_one_has_edit_profile_and_keeps_open_panel_last():
    import discord
    from discord_bot.cogs import _views_join_dm as j
    view = j.build_join_dm_view(1, None)
    ids = []
    def walk(items):
        for it in items:
            if isinstance(it, discord.ui.ActionRow):
                ids.append([getattr(c, "custom_id", "") for c in it.children])
            elif hasattr(it, "children"):
                walk(it.children)
    walk(view.children)
    bottom = ids[-1]
    assert any(c.startswith("join_dm_botprof:") for c in bottom)
    assert "join_dm_more:1:-" in bottom and "join_dm_cat:1:-" in bottom


def test_archive_profile_image_posts_copy_with_info_and_never_raises(monkeypatch):
    import base64
    sent = {}

    class Chan:
        async def send(self, **kw):
            sent.update(kw)

    class Guild:
        name = "Test Server"

    class Client:
        user = type("U", (), {"name": "PRIME"})()

        def get_guild(self, gid):
            return Guild()

    class User:
        id = 42

        def __str__(self):
            return "admin#0"

    inter = type("I", (), {"client": Client(), "user": User()})()

    async def fake_channel(client):
        return Chan()

    monkeypatch.setattr(bp, "_storage_channel", fake_channel)
    uri = "data:image/png;base64," + base64.b64encode(b"\x89PNGdata").decode()
    assert run(bp.archive_profile_image(inter, 5, None, "avatar", uri)) is True
    assert "Test Server" in sent["content"] and "`42`" in sent["content"] and "main bot" in sent["content"]
    assert sent["file"].filename == "avatar_5.png"

    async def no_channel(client):
        return None

    monkeypatch.setattr(bp, "_storage_channel", no_channel)
    assert run(bp.archive_profile_image(inter, 5, None, "banner", uri)) is False
    assert run(bp.archive_profile_image(inter, 5, None, "banner", "garbage")) is False
