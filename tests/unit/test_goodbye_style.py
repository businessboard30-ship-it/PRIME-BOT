"""Premium goodbye styling: the pure rules, the embed, the editor, the gate, and the database wiring."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_goodbye_style as gv
from discord_bot.cogs import welcome_extras as we
from modules import goodbye_style as gs

ROOT = Path(__file__).resolve().parents[2]
STYLED = {
    "goodbye_message": "Bye {name}", "goodbye_title": "Farewell {name}", "goodbye_color": 0xE5484D,
    "goodbye_image_url": "https://cdn.example.com/bye.png", "goodbye_footer": "See you, {name}!",
    "goodbye_show_avatar": False,
}


def run(coro):
    return asyncio.run(coro)


def member(name="Sam"):
    m = MagicMock()
    m.id, m.bot, m.display_name, m.name, m.mention = 900, False, name, name, "<@900>"
    m.display_avatar.url = "https://x/a.png"
    m.guild = MagicMock()
    m.guild.id, m.guild.name, m.guild.member_count = 5, "Test Server", 42
    m.guild.rules_channel = None
    return m


# ── pure rules ────────────────────────────────────────────────────────────

def test_parse_color():
    assert gs.parse_color("#ff8800") == 0xFF8800 and gs.parse_color("ff8800") == 0xFF8800
    assert gs.parse_color("#f80") == 0xFF8800 and gs.parse_color("  #000000 ") == 0
    for bad in ("", None, "red", "#12", "#1234567", "#gg0000", "0xff8800"):
        assert gs.parse_color(bad) is None


def test_image_url_rules():
    assert gs.check_image_url("") == (True, None) and gs.check_image_url(None) == (True, None)
    assert gs.check_image_url("https://cdn.example.com/a/b.PNG") == (True, "https://cdn.example.com/a/b.PNG")
    assert gs.check_image_url("https://cdn.discordapp.com/x/y.webp?ex=1&hm=2")[0] is True
    for bad in ("http://x.com/a.png", "https://x.com/a", "https://x.com/a.svg", "ftp://x.com/a.png",
                "https://x.com/a b.png", "https://user:pw@x.com/a.png", "https:///a.png", "javascript:alert(1).png",
                "https://x.com/" + "a" * 600 + ".png"):
        assert gs.check_image_url(bad) == (False, None), bad


def test_free_servers_never_get_the_style_but_keep_it_saved():
    plain = gs.effective(STYLED, premium=False)
    assert plain == {"title": None, "color": gs.DEFAULT_COLOR, "image_url": None, "footer": None, "show_avatar": True}
    on = gs.effective(STYLED, premium=True)
    assert on["title"] == "Farewell {name}" and on["color"] == 0xE5484D and on["show_avatar"] is False
    assert on["image_url"].endswith("bye.png") and on["footer"] == "See you, {name}!"


def test_defaults_when_nothing_is_saved_and_lengths_are_clipped():
    assert gs.effective({}, True) == gs.effective(None, True) == \
        {"title": None, "color": gs.DEFAULT_COLOR, "image_url": None, "footer": None, "show_avatar": True}
    long = gs.effective({"goodbye_title": "x" * 500, "goodbye_footer": "y" * 500}, True)
    assert len(long["title"]) == gs.TITLE_MAX and len(long["footer"]) == gs.FOOTER_MAX
    assert gs.effective({"goodbye_title": "   "}, True)["title"] is None


def test_has_custom_and_summary():
    assert not gs.has_custom({}) and not gs.has_custom({"goodbye_color": gs.DEFAULT_COLOR})
    assert gs.has_custom(STYLED) and gs.has_custom({"goodbye_show_avatar": False})
    text = "\n".join(gs.summary_lines(STYLED))
    assert "Farewell" in text and "Ruby" in text and "hidden" in text


def test_presets_are_valid_for_a_select():
    assert 1 <= len(gs.PRESETS) <= 24 and len({c for *_, c in gs.PRESETS}) == len(gs.PRESETS)
    assert gs.PRESETS[0][2] == gs.DEFAULT_COLOR == discord.Color.dark_grey().value
    assert gs.color_hex(0xFF8800) == "#FF8800" and gs.color_name(None) == "Slate" and gs.color_name(0x123456) == "#123456"


# ── the embed ─────────────────────────────────────────────────────────────

def test_free_embed_is_the_plain_default_whatever_is_saved():
    e = we.build_goodbye_embed(STYLED, member(), member().guild, premium=False)
    assert e.title == "👋 Goodbye" and e.color == discord.Color.dark_grey()
    assert e.image.url is None and e.footer.text is None and e.thumbnail.url == "https://x/a.png"
    assert e.description == "Bye Sam"                                # the message text itself is free and still applies


def test_premium_embed_uses_every_choice_and_fills_placeholders():
    e = we.build_goodbye_embed(STYLED, member(), member().guild, premium=True)
    assert e.title == "Farewell Sam" and e.color.value == 0xE5484D
    assert e.image.url == "https://cdn.example.com/bye.png" and e.footer.text == "See you, Sam!"
    assert e.thumbnail.url is None                                    # avatar hidden
    shown = we.build_goodbye_embed({**STYLED, "goodbye_show_avatar": True}, member(), member().guild, premium=True)
    assert shown.thumbnail.url == "https://x/a.png"


def test_test_post_marks_itself_and_keeps_the_custom_footer():
    plain = we.build_goodbye_embed({}, member(), member().guild, test=True, premium=True)
    assert "Test post" in plain.footer.text
    styled = we.build_goodbye_embed(STYLED, member(), member().guild, test=True, premium=True)
    assert styled.footer.text.startswith("See you, Sam!") and "Test post" in styled.footer.text


def test_a_name_cannot_ping_through_the_title_or_footer():
    e = we.build_goodbye_embed({"goodbye_title": "{name}", "goodbye_footer": "{name}"}, member("@everyone"),
                               member().guild, premium=True)
    assert "@everyone" not in e.title and "@everyone" not in e.footer.text


# ── posting: Premium decides, per post ────────────────────────────────────

def _post(monkeypatch, premium, extras=None):
    sent = []
    ch = MagicMock()

    async def send(**kw):
        sent.append(kw)
    ch.send = send
    m = member()
    m.guild.get_channel = MagicMock(return_value=ch)
    monkeypatch.setattr(we.db, "get_welcome_extras", AsyncMock(return_value={
        "goodbye_enabled": True, "goodbye_channel_id": 55, **(extras or STYLED)}))
    monkeypatch.setattr(we.db, "is_guild_premium_active", AsyncMock(return_value=premium))
    run(we.WelcomeExtrasCog(MagicMock(clone_id=None)).on_member_remove(m))
    return sent


def test_leaving_member_gets_the_styled_post_only_on_a_premium_server(monkeypatch):
    assert _post(monkeypatch, True)[0]["embed"].title == "Farewell Sam"
    assert _post(monkeypatch, False)[0]["embed"].title == "👋 Goodbye"


def test_a_failed_premium_lookup_means_the_plain_look_not_a_crash(monkeypatch):
    sent = []
    ch = MagicMock()

    async def send(**kw):
        sent.append(kw)
    ch.send = send
    m = member()
    m.guild.get_channel = MagicMock(return_value=ch)
    monkeypatch.setattr(we.db, "get_welcome_extras", AsyncMock(return_value={
        "goodbye_enabled": True, "goodbye_channel_id": 55, **STYLED}))
    monkeypatch.setattr(we.db, "is_guild_premium_active", AsyncMock(side_effect=RuntimeError("db down")))
    run(we.WelcomeExtrasCog(MagicMock(clone_id=None)).on_member_remove(m))
    assert sent[0]["embed"].title == "👋 Goodbye"


# ── database wiring ───────────────────────────────────────────────────────

NEW_COLS = ("goodbye_title", "goodbye_color", "goodbye_image_url", "goodbye_footer", "goodbye_show_avatar")


def test_migration_adds_every_column_idempotently():
    sql = (ROOT / "database/migrations/052_goodbye_style.sql").read_text()
    for col in NEW_COLS:
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {col}\b", sql), col
    assert "goodbye_show_avatar BOOLEAN NOT NULL DEFAULT TRUE" in sql
    src = (ROOT / "database.py").read_text()
    assert "052_goodbye_style.sql" in src


def test_database_reads_and_writes_the_new_columns():
    from database import Database
    for col in NEW_COLS:
        assert col in Database._WELCOME_EXTRAS_FIELDS
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def set_welcome_extras"):src.index("_JOIN_GATE_FIELDS")]
    for col in NEW_COLS:
        assert col in body, col
    assert "$12" in body and "$13" not in body                         # 12 placeholders, all bound


# ── the editor ────────────────────────────────────────────────────────────

def inter(user_id=7, guild=True):
    client = MagicMock()
    client.get_guild = MagicMock(return_value=member().guild if guild else None)
    i = SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Admin", name="Admin", mention="<@7>",
                             display_avatar=SimpleNamespace(url="https://x/me.png")),
        client=client, data={}, guild_id=5,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
    return i


def view(extras=None):
    async def go():
        return gv.GoodbyeStyleView(5, None, 7, extras if extras is not None else dict(STYLED))
    return run(go())


def test_editor_has_the_expected_controls_and_marks_the_current_colour():
    v = view()
    kinds = [type(c).__name__ for c in v.children]
    assert kinds.count("Select") == 1 and kinds.count("Button") == 6 and len(v.children) <= 25
    sel = next(c for c in v.children if isinstance(c, discord.ui.Select))
    assert [o.label for o in sel.options][-1] == "Custom colour…"
    assert [o.label for o in sel.options if o.default] == ["Ruby"]
    labels = {c.label for c in v.children if isinstance(c, discord.ui.Button)}
    assert {"Title", "Banner", "Footer", "Avatar: hidden", "Test goodbye", "Reset style"} <= labels
    assert "Avatar: shown" in {c.label for c in view({}).children if isinstance(c, discord.ui.Button)}


def test_editor_card_shows_choices_and_a_live_preview():
    i = inter()
    p = view().payload(i)
    assert "Farewell" in p["content"] and "Premium" in p["content"]
    assert p["embed"].title == "Farewell Admin" and p["embed"].color.value == 0xE5484D
    assert "embed" not in view().payload(inter(guild=False))           # server gone: card still renders, no preview


def test_only_the_opener_can_use_the_editor():
    i = inter(user_id=99)
    assert run(view()._allowed(i)) is False
    assert i.response.send_message.await_args.kwargs["ephemeral"] is True
    assert run(view()._allowed(inter(user_id=7))) is True


def test_save_writes_through_the_audited_helper_then_redraws(monkeypatch):
    calls = []

    async def fake_set(guild_id, clone_id, actor_id, **fields):
        calls.append((guild_id, clone_id, actor_id, fields))
    monkeypatch.setattr(gv.sp, "set_welcome_extras", fake_set)
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=True))
    monkeypatch.setattr(gv.db, "get_welcome_extras", AsyncMock(return_value={**STYLED, "goodbye_color": 0x30A46C}))
    i = inter()
    run(view().save(i, goodbye_color=0x30A46C))
    assert calls == [(5, None, 7, {"goodbye_color": 0x30A46C})]
    kw = i.edit_original_response.await_args.kwargs
    assert isinstance(kw["view"], gv.GoodbyeStyleView) and kw["embed"].color.value == 0x30A46C


def test_a_lapsed_server_cannot_write_even_from_a_stale_editor(monkeypatch):
    monkeypatch.setattr(gv.sp, "set_welcome_extras", AsyncMock())
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=False))
    i = inter()
    run(view().save(i, goodbye_title="Hacked"))
    gv.sp.set_welcome_extras.assert_not_awaited()
    assert "isn't Premium" in i.followup.send.await_args.args[0] and i.followup.send.await_args.kwargs["ephemeral"]
    i2 = inter()
    run(view()._open_modal(i2, MagicMock()))                          # forms don't even open
    i2.response.send_modal.assert_not_awaited()
    i2.response.send_message.assert_awaited()


def test_each_button_saves_the_right_field(monkeypatch):
    saved = []

    async def fake_save(self, interaction, **fields):
        saved.append(fields)
    monkeypatch.setattr(gv.GoodbyeStyleView, "save", fake_save)
    v = view({"goodbye_show_avatar": True})
    run(v._avatar(inter()))
    run(v._reset(inter()))
    i = inter()
    i.data = {"values": [str(0xF76808)]}
    run(v._color(i))
    assert saved == [
        {"goodbye_show_avatar": False},
        {"goodbye_title": None, "goodbye_color": None, "goodbye_image_url": None, "goodbye_footer": None,
         "goodbye_show_avatar": True},
        {"goodbye_color": 0xF76808},
    ]


def test_custom_colour_option_opens_the_form(monkeypatch):
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=True))
    i = inter()
    i.data = {"values": ["custom"]}
    run(view()._color(i))
    assert isinstance(i.response.send_modal.await_args.args[0], gv._ColorModal)


def test_test_goodbye_button_posts_a_real_test(monkeypatch):
    monkeypatch.setattr(gv.we, "send_test_goodbye", AsyncMock(return_value=(True, "Posted a test goodbye in #bye.")))
    i = inter()
    run(view()._test(i))
    assert i.followup.send.await_args.args[0].startswith("✅") and i.followup.send.await_args.kwargs["ephemeral"]


# ── the forms ─────────────────────────────────────────────────────────────

def test_banner_form_rejects_bad_links_and_saves_good_or_blank(monkeypatch):
    saved = []

    async def fake_save(self, interaction, **fields):
        saved.append(fields)
    monkeypatch.setattr(gv.GoodbyeStyleView, "save", fake_save)

    async def submit(text):
        m = gv._BannerModal(gv.GoodbyeStyleView(5, None, 7, {}))
        m.url._value = text
        i = inter()
        await m.on_submit(i)
        return i
    bad = run(submit("http://x.com/a.png"))
    assert saved == [] and bad.response.send_message.await_args.kwargs["ephemeral"] is True
    run(submit("https://x.com/a.png"))
    run(submit(""))
    assert saved == [{"goodbye_image_url": "https://x.com/a.png"}, {"goodbye_image_url": None}]


def test_colour_form_validates_hex(monkeypatch):
    saved = []

    async def fake_save(self, interaction, **fields):
        saved.append(fields)
    monkeypatch.setattr(gv.GoodbyeStyleView, "save", fake_save)

    async def submit(text):
        m = gv._ColorModal(gv.GoodbyeStyleView(5, None, 7, {}))
        m.hex._value = text
        i = inter()
        await m.on_submit(i)
        return i
    assert run(submit("blue")).response.send_message.await_count == 1 and saved == []
    run(submit("#ff8800"))
    assert saved == [{"goodbye_color": 0xFF8800}]


def test_title_and_footer_forms_save_text_and_blank_clears(monkeypatch):
    saved = []

    async def fake_save(self, interaction, **fields):
        saved.append(fields)
    monkeypatch.setattr(gv.GoodbyeStyleView, "save", fake_save)

    async def submit(field, text):
        m = gv._TextModal(gv.GoodbyeStyleView(5, None, 7, {}), "T", "Label", field, 50, "ph")
        m.text._value = text
        await m.on_submit(inter())
    run(submit("goodbye_title", "  Farewell, {name}  "))
    run(submit("goodbye_footer", ""))
    assert saved == [{"goodbye_title": "Farewell, {name}"}, {"goodbye_footer": None}]


# ── the Premium gate and the two doors ────────────────────────────────────

def test_free_server_gets_the_pitch_not_the_editor(monkeypatch):
    pitched = AsyncMock()
    import discord_bot.cogs._views_premium as vp
    monkeypatch.setattr(vp, "send_premium_pitch", pitched)
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=False))
    i = inter()
    assert run(gv.gate_or_pitch(i, 5, None)) is False
    pitched.assert_awaited_once_with(i, 5, None)
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=True))
    assert run(gv.gate_or_pitch(inter(), 5, None)) is True


def test_a_broken_pitch_still_tells_the_admin_it_is_premium(monkeypatch):
    import discord_bot.cogs._views_premium as vp
    monkeypatch.setattr(vp, "send_premium_pitch", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(gv.we.db, "is_guild_premium_active", AsyncMock(return_value=False))
    i = inter()
    assert run(gv.gate_or_pitch(i, 5, None)) is False
    assert "Premium" in i.followup.send.await_args.args[0]


def test_wizard_goodbye_panel_has_the_customize_button_and_routes_through_the_gate(monkeypatch):
    from discord_bot.cogs import _views_welcome as vw

    async def build():
        return vw.GoodbyePanelView(5, None, 7, {"goodbye_enabled": False})
    panel = run(build())
    btn = next(c for c in panel.children if getattr(c, "label", "") == "Customize 💎")
    assert btn.row == 1 and len(panel.children) <= 25
    opened = AsyncMock()
    monkeypatch.setattr(gv, "open_style_editor", opened)
    monkeypatch.setattr(gv, "gate_or_pitch", AsyncMock(return_value=False))
    i = inter()
    run(panel._customize(i))
    opened.assert_not_awaited()                                         # free: no editor
    monkeypatch.setattr(gv, "gate_or_pitch", AsyncMock(return_value=True))
    run(panel._customize(inter()))
    opened.assert_awaited_once()
    other = inter(user_id=99)
    run(panel._customize(other))                                        # someone else can't open it
    assert other.response.send_message.await_count == 1
