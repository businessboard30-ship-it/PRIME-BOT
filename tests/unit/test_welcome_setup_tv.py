"""/welcome setup wizard: the TV. Every option redraws the real card (from the saved settings) inside the wizard
message; a failed redraw keeps the old picture; the wizard itself always still updates."""
import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from discord_bot.cogs import _views_welcome as vw


def galleries(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.MediaGallery)]


def png(w=1400, h=500):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(b, format="PNG")
    return b.getvalue()


# ---------- the view ----------

def test_view_without_a_picture_is_unchanged():
    assert not galleries(vw.build_wizard_view(1, None, 5, {}))


def test_view_with_a_picture_points_at_the_attachment_under_the_header():
    v = vw.build_wizard_view(1, None, 5, {}, tv_name="wtv-abc123.png")
    g = galleries(v)
    assert len(g) == 1 and g[0].items[0].media.url == "attachment://wtv-abc123.png"
    kids = list(v.walk_children())
    texts = [i for i, c in enumerate(kids) if isinstance(c, discord.ui.TextDisplay)]
    assert kids.index(g[0]) > texts[0]                                # after the header text
    assert any("Live preview" in c.content for c in kids if isinstance(c, discord.ui.TextDisplay))


def test_both_modes_stay_inside_discords_component_limit():
    for cfg in ({"use_template": True}, {"use_template": False}):
        v = vw.build_wizard_view(1, None, 5, cfg, tv_name="wtv-a.png", goodbye=None)
        assert len(list(v.walk_children())) + 1 <= 40, cfg            # +1 for the container itself


def test_animated_style_is_called_out_as_a_still_frame():
    assert "still frame" in vw._tv_caption({"use_template": False, "card_style": "gif"})
    assert "still frame" not in vw._tv_caption({"use_template": True})
    assert "still frame" not in vw._tv_caption({"use_template": False, "card_style": "static"})


# ---------- the picture ----------

def test_picture_is_a_downscaled_still_png_even_for_an_animated_card():
    out = Image.open(io.BytesIO(vw._tv_png(png())))
    assert out.format == "PNG" and out.width == vw._TV_WIDTH
    frames = [Image.new("RGB", (400, 150), c) for c in ("red", "blue")]
    g = io.BytesIO()
    frames[0].save(g, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    still = Image.open(io.BytesIO(vw._tv_png(g.getvalue())))
    assert still.format == "PNG" and getattr(still, "n_frames", 1) == 1


def test_every_picture_gets_a_new_file_name():
    names = {vw._new_tv_name() for _ in range(50)}
    assert len(names) == 50 and all(n.startswith("wtv-") and n.endswith(".png") for n in names)


def test_existing_picture_is_found_on_a_message_by_its_prefix():
    msg = SimpleNamespace(attachments=[SimpleNamespace(filename="other.png"), SimpleNamespace(filename="wtv-x1.png")])
    assert vw._existing_tv_name(msg) == "wtv-x1.png"
    assert vw._existing_tv_name(SimpleNamespace(attachments=[])) is None
    assert vw._existing_tv_name(None) is None


def test_tv_renders_the_saved_settings_like_a_real_join(monkeypatch):
    seen = {}

    def fake_render(avatar, name, member_line, **kw):
        seen.update(kw, avatar=avatar, name=name, member_line=member_line)
        return png(), "PNG"

    class Resp:
        async def read(self):
            return b"avatar-bytes"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class Sess:
        def get(self, *a, **k):
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    async def no_sticker(session, url):
        return None

    async def no_bg(session, cfg, client):
        return b"custom-bg"
    from discord_bot.cogs import welcome
    monkeypatch.setattr(vw, "render_welcome_card", fake_render)
    monkeypatch.setattr(vw.aiohttp, "ClientSession", lambda *a, **k: Sess())
    monkeypatch.setattr(vw, "_fetch_sticker_bytes", no_sticker)
    monkeypatch.setattr(welcome, "_custom_bg_bytes_for_render", no_bg)
    vw._TV_AVATARS.clear()
    asset = SimpleNamespace(key="k1", replace=lambda size: SimpleNamespace(url="https://x/a.png"))
    user = SimpleNamespace(id=9, display_name="Ann", display_avatar=asset)
    guild = SimpleNamespace(name="Guild", member_count=12)
    cfg = {"use_template": True, "card_theme": "reaper", "avatar_shape": "hexagon", "background_color": "#111111",
           "accent_color": "#222222", "card_style": "gif", "ultra_card_json": "{}"}
    out = asyncio.run(vw._render_setup_tv(None, guild, user, cfg))
    assert Image.open(io.BytesIO(out)).format == "PNG"
    assert seen["theme"] == "reaper" and seen["avatar_shape"] == "hexagon" and seen["use_template"] is True
    assert seen["animate"] is False and seen["guild_name"] == "Guild" and seen["member_line"] == "Member #12"
    assert seen["custom_background_bytes"] == b"custom-bg" and seen["ultra_options"] == "{}" and seen["name"] == "Ann"
    assert seen["avatar"] == b"avatar-bytes"


def test_unavailable_server_raises_instead_of_drawing_nonsense():
    with pytest.raises(RuntimeError):
        asyncio.run(vw._render_setup_tv(None, None, SimpleNamespace(id=1), {}))


# ---------- redraw behaviour (fake Discord interaction) ----------

class FakeInteraction:
    def __init__(self, msg_id=77, on_message=None):
        self.user = SimpleNamespace(id=42, display_name="Ann")
        self.guild = SimpleNamespace(name="G", member_count=3)
        self.client = SimpleNamespace(get_guild=lambda gid: self.guild)
        self.message = SimpleNamespace(id=msg_id, attachments=[SimpleNamespace(filename=n) for n in (on_message or [])])
        self.live = list(on_message or [])
        self.edits = []
        self.fail_attach = None

    async def edit_original_response(self, **kw):
        if kw.get("attachments") and self.fail_attach:
            raise self.fail_attach
        self.edits.append(kw)
        if "attachments" in kw:
            self.live = [f.filename for f in kw["attachments"]]

    async def original_response(self):
        return SimpleNamespace(attachments=[SimpleNamespace(filename=n) for n in self.live])


@pytest.fixture
def tv(monkeypatch):
    state = {"renders": 0, "fail": False}

    async def render(client, guild, user, config):
        state["renders"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return png()

    async def get_cfg(gid, clone_id=None):
        return {"use_template": True, "card_theme": "wolf"}

    async def goodbye(gid, cid):
        return None
    monkeypatch.setattr(vw, "_render_setup_tv", render)
    monkeypatch.setattr(vw.db, "get_welcome_config", get_cfg, raising=False)
    monkeypatch.setattr(vw, "fetch_goodbye", goodbye)
    monkeypatch.setattr(vw, "_TV_DELAY", 0.01)
    vw._TV_GEN.clear()
    vw._TV_LOCKS.clear()
    return state


def run(i):
    asyncio.run(vw._rerender(i, 1, None, 5))


def test_every_change_redraws_the_tv_and_the_controls_in_one_edit(tv):
    i = FakeInteraction()
    run(i)
    assert len(i.edits) == 1 and tv["renders"] == 1
    kw = i.edits[0]
    name = kw["attachments"][0].filename
    assert name.startswith("wtv-") and galleries(kw["view"])[0].items[0].media.url == f"attachment://{name}"


def test_each_redraw_uses_a_new_file_name(tv):
    i = FakeInteraction()
    run(i)
    run(i)
    assert i.edits[0]["attachments"][0].filename != i.edits[1]["attachments"][0].filename


def test_quick_taps_collapse_into_one_redraw(tv):
    async def go():
        i = FakeInteraction()
        await asyncio.gather(*[vw._rerender(i, 1, None, 5) for _ in range(4)])
        return i
    i = asyncio.run(go())
    assert tv["renders"] == 1 and len(i.edits) == 1


def test_failed_redraw_keeps_the_picture_already_there_and_still_updates_the_controls(tv):
    tv["fail"] = True
    i = FakeInteraction(on_message=["wtv-old111.png"])
    run(i)
    assert len(i.edits) == 1 and "attachments" not in i.edits[0]            # attachments untouched
    assert galleries(i.edits[0]["view"])[0].items[0].media.url == "attachment://wtv-old111.png"


def test_failed_redraw_uses_the_live_message_not_a_stale_click_payload(tv):
    tv["fail"] = True
    i = FakeInteraction(on_message=["wtv-stale.png"])
    i.live = ["wtv-fresh.png"]                       # another edit landed after this click arrived
    run(i)
    assert galleries(i.edits[0]["view"])[0].items[0].media.url == "attachment://wtv-fresh.png"


def test_failed_redraw_with_no_picture_shows_the_wizard_without_a_tv(tv):
    tv["fail"] = True
    i = FakeInteraction()
    run(i)
    assert len(i.edits) == 1 and "attachments" not in i.edits[0] and not galleries(i.edits[0]["view"])


def test_cannot_attach_files_still_updates_the_wizard(tv):
    i = FakeInteraction(on_message=["wtv-old111.png"])
    i.fail_attach = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "Missing Permissions")
    run(i)
    assert len(i.edits) == 1 and "attachments" not in i.edits[0]
    assert galleries(i.edits[0]["view"])[0].items[0].media.url == "attachment://wtv-old111.png"


def test_a_plain_edit_failure_without_a_picture_is_not_swallowed(tv):
    tv["fail"] = True
    i = FakeInteraction()

    async def boom(**kw):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "server error")
    i.edit_original_response = boom
    with pytest.raises(discord.HTTPException):
        run(i)


# ---------- opening the wizard, and refresh by the standalone commands ----------

def test_opening_the_wizard_draws_the_tv_first(tv):
    i = FakeInteraction()
    view, kwargs = asyncio.run(vw.open_wizard_message(i, 1, None, 5, {"use_template": True}))
    assert len(kwargs["files"]) == 1 and galleries(view)[0].items[0].media.url == f"attachment://{kwargs['files'][0].filename}"


def test_opening_the_wizard_still_works_when_the_picture_cannot_be_drawn(tv):
    tv["fail"] = True
    view, kwargs = asyncio.run(vw.open_wizard_message(FakeInteraction(), 1, None, 5, {}))
    assert kwargs == {} and not galleries(view)


class FakeMessage:
    def __init__(self, names):
        self.id = 900
        self.attachments = [SimpleNamespace(filename=n) for n in names]
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


def refresh(monkeypatch, tv, message, invoker, member_present):
    async def get_cfg(gid, clone_id=None):
        return {"wizard_channel_id": 5, "wizard_message_id": 900, "wizard_invoker_id": invoker, "use_template": True}
    monkeypatch.setattr(vw.db, "get_welcome_config", get_cfg, raising=False)

    class Chan:
        async def fetch_message(self, mid):
            return message
    guild = SimpleNamespace(name="G", member_count=3,
                            get_member=lambda uid: SimpleNamespace(id=uid, display_name="A") if member_present else None)
    bot = SimpleNamespace(get_channel=lambda cid: Chan(), get_guild=lambda gid: guild)
    asyncio.run(vw.refresh_posted_wizard(bot, 1, None))


def test_refresh_redraws_as_the_person_who_opened_the_wizard(monkeypatch, tv):
    m = FakeMessage(["wtv-old111.png"])
    refresh(monkeypatch, tv, m, invoker=42, member_present=True)
    assert tv["renders"] == 1 and len(m.edits) == 1
    new = m.edits[0]["attachments"][0].filename
    assert new != "wtv-old111.png" and galleries(m.edits[0]["view"])[0].items[0].media.url == f"attachment://{new}"


def test_refresh_without_a_known_person_keeps_the_existing_picture(monkeypatch, tv):
    m = FakeMessage(["wtv-old111.png"])
    refresh(monkeypatch, tv, m, invoker=None, member_present=False)
    assert tv["renders"] == 0 and "attachments" not in m.edits[0]
    assert galleries(m.edits[0]["view"])[0].items[0].media.url == "attachment://wtv-old111.png"


def test_refresh_never_raises_when_the_picture_fails(monkeypatch, tv):
    tv["fail"] = True
    m = FakeMessage(["wtv-old111.png"])
    refresh(monkeypatch, tv, m, invoker=42, member_present=True)
    assert len(m.edits) == 1 and "attachments" not in m.edits[0]


def test_callers_that_have_no_person_still_build_the_wizard_without_a_tv():
    # the auto-posted on-join wizard has no user; it must keep working with no picture until the first tap
    v = vw.build_wizard_view(1, None, None, {}, greeting="hi", goodbye=None)
    assert not galleries(v)
