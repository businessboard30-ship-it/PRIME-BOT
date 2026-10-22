"""Welcome Card Studio Phase 9: non-Latin name note (9a), Compare with live (9b), tweak marker on the picture strip (9c)."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc

KEY = (1, None, 42)


def run(c):
    return asyncio.run(c)


def comps(v):
    return list(v.walk_children())


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(v) if getattr(c, "custom_id", None)}


# ---------- 9a: non-Latin name note ----------

def test_note_only_when_a_style_font_cannot_draw_the_name():
    assert wc.style_font_fallback_note("Sam", {"font": "gothic"}) == ""                 # Latin: fine
    assert wc.style_font_fallback_note("Zoë", {"font": "gothic"}) == ""                 # Latin-1 / extended Latin: fine
    assert wc.style_font_fallback_note("ひらがな", {"font": "classic"}) == ""            # normal font already: nothing to say
    msg = wc.style_font_fallback_note("ひらがな", {"font": "gothic"})
    assert "normal font" in msg and len(msg) < 120


def test_note_accepts_stored_json_and_never_raises():
    assert "normal font" in wc.style_font_fallback_note("Привет", '{"font": "gothic"}')
    for junk in (None, "", 5, "not json", {"font": object()}):
        assert isinstance(wc.style_font_fallback_note("Привет", junk), str)
    assert wc.style_font_fallback_note(None, {"font": "gothic"}) == ""
    assert wc.style_font_fallback_note("", {"font": "gothic"}) == ""


def _tv(monkeypatch, name, ultra):
    fake_db = MagicMock()
    fake_db.get_welcome_config = AsyncMock(return_value={"ultra_pack_unlocked": True, "ultra_card_json": ultra})
    monkeypatch.setattr(cc, "db", fake_db)
    monkeypatch.setattr(cc, "render_welcome_card", lambda *a, **k: (b"CARD", "PNG"))
    monkeypatch.setattr(cc, "_avatar_bytes", AsyncMock(return_value=b"AV"))
    monkeypatch.setattr(cc, "_gallery_preview_pick", AsyncMock(return_value=(b"BG", {})))
    monkeypatch.setattr(cc, "_tv_finish", lambda b, wm, full=False: b)
    monkeypatch.setattr(cc, "_effective_config", lambda real, g, c, u: dict(real))
    guild = MagicMock(member_count=9)
    guild.name = "G"
    user = MagicMock(id=42, display_name=name)
    return run(cc._render_tv(MagicMock(), guild, 1, None, user))


def test_preview_note_says_it_for_a_non_latin_name_with_a_style_font(monkeypatch):
    cc._PREVIEW_AS.clear()
    _png, note = _tv(monkeypatch, "ひらがな", '{"font": "gothic"}')
    assert "normal font" in note
    _png, note = _tv(monkeypatch, "Sam", '{"font": "gothic"}')
    assert "normal font" not in note
    _png, note = _tv(monkeypatch, "ひらがな", None)
    assert "normal font" not in note


def test_preview_as_name_is_the_one_checked(monkeypatch):
    cc._PREVIEW_AS[KEY] = {"slot": None, "name": "Привет", "number": None}
    try:
        _png, note = _tv(monkeypatch, "Admin", '{"font": "gothic"}')
        assert "normal font" in note
    finally:
        cc._PREVIEW_AS.clear()


# ---------- 9c: tweak marker ----------

def _strip(studio):
    return cc._gallery_line(studio)


def test_strip_marks_only_pictures_that_have_a_tweak():
    st = {"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "tweaked": [2], "mode": "random"}
    line = _strip(st)
    assert "2\u25CF\U0001F39A" in line and "1\u25CF " in line and "3\u25CF " in line
    assert "own tweak" in line


def test_strip_is_unchanged_when_nothing_is_tweaked():
    st = {"unlocked": True, "pictures": 2, "needs": [], "slots": [1, 2], "mode": "random"}
    assert "\U0001F39A" not in _strip(st) and "own tweak" not in _strip(st)
    assert "\U0001F39A" not in _strip({**st, "tweaked": []})


def test_strip_never_marks_an_empty_slot():
    st = {"unlocked": True, "pictures": 1, "needs": [], "slots": [1], "tweaked": [5], "mode": "random"}
    assert "5\u25CB\U0001F39A" not in _strip(st)


def test_with_studio_lists_tweaked_slots(monkeypatch):
    rows = [{"slot": 1, "options_override_json": None},
            {"slot": 2, "options_override_json": '{"focus": "top"}'},
            {"slot": 3, "options_override_json": "junk"}]
    fake_db = MagicMock()
    fake_db.gallery_list = AsyncMock(return_value=rows)
    fake_db.design_list = AsyncMock(return_value=[])
    monkeypatch.setattr(cc, "db", fake_db)
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    out = run(cc._with_studio({}, 1, None, 42))
    assert out["_studio"]["tweaked"] == [2]


# ---------- 9b: Compare with live ----------

def build_send(unlocked=True, trial=False):
    cfg = {"ultra_pack_unlocked": unlocked, "ultra_trial_active": trial}
    return cc.build_customize_view(1, None, 42, cfg, tab="send")


def test_compare_button_is_on_the_send_step_for_unlocked_servers_only():
    assert "compare" in kinds(build_send(True))
    assert "compare" not in kinds(build_send(False))
    for tab in ("bg", "layout", "text"):
        for more in (False, True):
            v = cc.build_customize_view(1, None, 42, {"ultra_pack_unlocked": True}, tab=tab, more=more)
            assert "compare" not in kinds(v)


def test_send_step_stays_inside_discord_limits_even_with_a_trial():
    for v in (build_send(True), build_send(True, trial=True), build_send(False)):
        assert len(comps(v)) <= 40
        for row in (c for c in comps(v) if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


def test_compare_is_registered_and_round_trips():
    cls = cc.CardCompareButton
    assert cls in cc.DYNAMIC_ITEMS
    for clone, inv in ((None, 5), (7, None)):
        b = cls(123, clone, inv)
        m = re.match(cls.__discord_ui_compiled_template__.pattern, b.item.custom_id)
        again = run(cls.from_custom_id(None, None, m))
        assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


class It:
    def __init__(self):
        self.user = SimpleNamespace(id=42)
        self.guild = SimpleNamespace(name="G", member_count=3)
        self.client = SimpleNamespace(get_guild=lambda g: self.guild)
        self.deferred, self.sent, self.said = [], [], []
        self.response = SimpleNamespace(defer=self._defer, send_message=self._say)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, **k):
        self.deferred.append(k)

    async def _say(self, *a, **k):
        self.said.append((a, k))

    async def _send(self, *a, **k):
        self.sent.append((a, k))


@pytest.fixture
def world(monkeypatch):
    st = {"cfg": {"ultra_pack_unlocked": True}, "calls": [], "access": True, "png": {False: b"MINE", True: b"LIVE"}}

    async def get(gid, clone_id=None):
        return dict(st["cfg"])

    async def access(*a, **k):
        return st["access"]

    async def render(client, guild, gid, clone_id, user, full=False, live=False):
        st["calls"].append({"full": full, "live": live})
        return st["png"][live], "note"
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    monkeypatch.setattr(cc, "_check_access", access)
    monkeypatch.setattr(cc, "_render_tv", render)
    cc._TEST_LAST.clear()
    return st


def text(sent):
    a, kw = sent
    return str(a[0] if a else kw.get("content", ""))


def press(item):
    it = It()
    run(item.callback(it))
    return it


def test_compare_sends_both_cards_privately_when_they_differ(world):
    it = press(cc.CardCompareButton(1, None, 42))
    assert [c["live"] for c in world["calls"]] == [False, True]
    assert it.deferred[0].get("ephemeral") is True
    (_a, kw), = it.sent
    assert kw["ephemeral"] is True
    assert [f.filename for f in kw["files"]] == ["compare-editor.png", "compare-live.png"]
    assert "live today" in kw["content"].lower()


def test_compare_says_so_when_the_cards_are_the_same(world):
    world["png"] = {False: b"X", True: b"X"}
    it = press(cc.CardCompareButton(1, None, 42))
    assert len(it.sent) == 1
    assert "same card" in text(it.sent[0]).lower() and "files" not in it.sent[0][1]


def test_compare_on_an_unpaid_server_renders_nothing(world):
    world["cfg"] = {"ultra_pack_unlocked": False}
    it = press(cc.CardCompareButton(1, None, 42))
    assert world["calls"] == []
    assert "nothing is saved" in text(it.sent[0]).lower()


def test_compare_needs_access_and_has_a_cooldown(world):
    world["access"] = False
    it = press(cc.CardCompareButton(1, None, 42))
    assert world["calls"] == [] and it.deferred == [] and not it.sent
    world["access"] = True
    press(cc.CardCompareButton(1, None, 42))
    n = len(world["calls"])
    it = press(cc.CardCompareButton(1, None, 42))
    assert len(world["calls"]) == n and it.said and it.said[0][1]["ephemeral"] is True


def test_compare_failure_is_a_friendly_message(world, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(cc, "_render_tv", boom)
    it = press(cc.CardCompareButton(1, None, 42))
    assert "couldn't draw" in text(it.sent[0]).lower()


def test_live_render_uses_the_join_gate_and_ignores_in_memory_preview(monkeypatch):
    """live=True: options and shape come from the same helpers as the real join, never from the editor overlay."""
    got = {}

    def fake_render(avatar, name, subtitle, **k):
        got.update(k)
        return b"CARD", "PNG"

    fake_db = MagicMock()
    fake_db.get_welcome_config = AsyncMock(return_value={
        "ultra_pack_unlocked": True, "guild_id": 1, "ultra_card_json": '{"frame": "thick"}', "avatar_shape": "circle"})
    monkeypatch.setattr(cc, "db", fake_db)
    monkeypatch.setattr(cc, "render_welcome_card", fake_render)
    monkeypatch.setattr(cc, "_avatar_bytes", AsyncMock(return_value=b"AV"))
    monkeypatch.setattr(cc, "_gallery_preview_pick", AsyncMock(return_value=(b"BG", {})))
    monkeypatch.setattr(cc, "_tv_finish", lambda b, wm, full=False: b)
    overlay = MagicMock(side_effect=AssertionError("the editor overlay must not be used for the live card"))
    monkeypatch.setattr(cc, "_effective_config", overlay)
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "options_for_render", AsyncMock(return_value={"frame": "none"}))
    monkeypatch.setattr(ws, "avatar_shape_for_render", AsyncMock(return_value="square"))
    guild = MagicMock(member_count=9)
    guild.name = "G"
    user = MagicMock(id=42, display_name="Sam")
    run(cc._render_tv(MagicMock(), guild, 1, None, user, live=True))
    assert got["avatar_shape"] == "square"
    assert wc.parse_ultra_options(got["ultra_options"])["frame"] == "none"
    overlay.assert_not_called()


def test_live_flag_defaults_off_so_every_old_caller_is_unchanged(monkeypatch):
    import inspect
    assert inspect.signature(cc._render_tv).parameters["live"].default is False
