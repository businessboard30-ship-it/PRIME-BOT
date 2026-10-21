"""Welcome Card Studio, Phase 1: the 4-step shell, Easy / More controls, Undo, Reset this step, Send me a test.
No new card features: the stored options and the renderer are unchanged."""
import asyncio
import json
import re
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_card_customize as cc
from modules.welcome_card import parse_ultra_options

KEY = (1, None, 42)


def comps(view):
    return list(view.walk_children())


def kinds(view):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(view) if getattr(c, "custom_id", None)}


def build(tab, more=False, unlocked=True, bg=True):
    cfg = {"ultra_pack_unlocked": unlocked}
    if bg:
        cfg["custom_background_url"] = "https://x/y.png"
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


def control_rows(view):
    """Rows that hold a real control (not the step switcher, the presets or the footer)."""
    out = []
    for row in (c for c in comps(view) if isinstance(c, discord.ui.ActionRow)):
        k = {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in row.children}
        if k & {"tabbg", "preset", "undo", "testsend"}:
            continue
        out.append(k)
    return out


# ---------- the shell ----------

def test_four_numbered_steps_in_the_right_order():
    assert cc._TABS == ("bg", "layout", "text", "send")
    labels = [cc._TAB_LABELS[t] for t in cc._TABS]
    assert [l.split()[0] for l in labels] == ["1", "2", "3", "4"]
    assert [l.split()[-1] for l in labels] == ["Pictures", "Look", "Text", "Send"]
    assert all(len(l) <= 80 for l in labels)


def test_every_step_has_a_plain_words_hint_and_the_step_buttons():
    for t in cc._TABS:
        v = build(t)
        text = " ".join(getattr(c, "content", "") for c in comps(v))
        assert f"Step {cc._TABS.index(t) + 1} of 4" in text
        assert {"tabbg", "tablayout", "tabtext", "tabsend"} <= kinds(v)


def test_old_wizard_messages_keep_working():
    pat = re.compile(cc.CardTabButton.__discord_ui_compiled_template__.pattern)
    for old in ("cardwz_tabbg:1:-:5", "cardwz_tablayout:1:7:-", "cardwz_tabtext:1:-:-", "cardwz_tabsend:1:-:5"):
        assert pat.match(old), old
    # every class from before is still registered, plus the new ones
    for cls in (cc.CardResetButton, cc.CardPreviewButton, cc.CardDoneButton, cc.CardBackgroundButton,
                cc.CardMoreButton, cc.CardUndoButton, cc.CardStepResetButton, cc.CardTestSendButton):
        assert cls in cc.DYNAMIC_ITEMS


def test_new_buttons_round_trip_their_ids_for_main_and_clone():
    for cls in (cc.CardMoreButton, cc.CardUndoButton, cc.CardStepResetButton, cc.CardTestSendButton):
        for clone, inv in ((None, 5), (7, None)):
            b = cls(123, clone, inv)
            m = re.match(cls.__discord_ui_compiled_template__.pattern, b.item.custom_id)
            again = asyncio.run(cls.from_custom_id(None, None, m))
            assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


# ---------- Easy / More controls ----------

def test_easy_mode_shows_at_most_three_controls_per_step():
    for t in ("bg", "layout", "text"):
        assert len(control_rows(build(t))) <= 3, t


def test_more_controls_adds_that_steps_extras_only():
    assert "focus" not in kinds(build("bg")) and "focus" in kinds(build("bg", more=True))
    assert "shape" not in kinds(build("layout")) and "shape" in kinds(build("layout", more=True))
    t_easy, t_more = kinds(build("text")), kinds(build("text", more=True))
    assert not ({"bracket", "style"} & t_easy) and {"bracket", "style"} <= t_more
    # nothing from another step leaks in
    assert not ({"ratio", "banner"} & t_more) and not ({"color", "font"} & kinds(build("layout", more=True)))


def test_more_mode_stays_inside_discord_limits_on_every_step():
    for t in cc._TABS:
        for unlocked in (True, False):
            v = cc.build_customize_view(1, 7, 42, {"ultra_pack_unlocked": unlocked, "ultra_trial_active": True,
                                                    "custom_background_url": "https://x/y.png"},
                                        tab=t, has_tv=True, note="x", more=True)
            assert len(comps(v)) < 40, t
            assert sum(isinstance(c, discord.ui.Select) for c in comps(v)) <= 5, t
            for row in (c for c in comps(v) if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5


def test_every_control_step_has_undo_reset_and_more_and_send_has_test_and_reset_all():
    for t in ("bg", "layout", "text"):
        assert {"undo", "stepreset", "more"} <= kinds(build(t))
    assert {"testsend", "reset"} <= kinds(build("send")) and "undo" not in kinds(build("send"))


def test_more_button_label_follows_its_state():
    assert "More" in cc.CardMoreButton(1, None, 5, False).item.label
    assert "Fewer" in cc.CardMoreButton(1, None, 5, True).item.label


def test_more_is_remembered_per_step_and_expires():
    cc._MORE_STATE.clear()
    assert not cc._more_open(KEY, "text")
    assert cc._toggle_more(KEY, "text") is True and cc._more_open(KEY, "text") and not cc._more_open(KEY, "layout")
    assert cc._toggle_more(KEY, "text") is False and not cc._more_open(KEY, "text")
    cc._toggle_more(KEY, "layout")
    cc._MORE_STATE[KEY] = (cc._MORE_STATE[KEY][0], cc._MORE_STATE[KEY][1] - cc._TAB_TTL - 1)
    assert not cc._more_open(KEY, "layout")


# ---------- fake database + interaction ----------

class It:
    def __init__(self, uid=42):
        self.user = SimpleNamespace(id=uid)
        self.guild = SimpleNamespace(name="G", member_count=3)
        self.client = SimpleNamespace(get_guild=lambda g: self.guild)
        self.deferred, self.sent, self.edits, self.said = [], [], [], []
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
    st = {"cfg": {"ultra_pack_unlocked": True, "avatar_shape": "circle"}, "writes": [], "rerenders": 0, "access": True,
          "renders": []}

    async def get(gid, clone_id=None):
        return dict(st["cfg"])

    async def setc(gid, clone_id=None, **kw):
        st["writes"].append(kw)
        st["cfg"].update(kw)

    async def rerender(*a, **k):
        st["rerenders"] += 1

    async def access(*a, **k):
        return st["access"]

    async def render(client, guild, gid, clone_id, user, full=False):
        st["renders"].append(full)
        return b"PNGDATA", "Your background"
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    monkeypatch.setattr(cc.db, "set_welcome_config", setc)
    monkeypatch.setattr(cc, "_rerender", rerender)
    monkeypatch.setattr(cc, "_check_access", access)
    monkeypatch.setattr(cc, "_render_tv", render)
    for d in (cc._DRAFTS, cc._UNDO, cc._MORE_STATE, cc._TAB_STATE, cc._TEST_LAST):
        d.clear()
    return st


def press(item, uid=42):
    it = It(uid)
    asyncio.run(item.callback(it))
    return it


# ---------- Undo ----------

def test_undo_restores_the_previous_look_for_a_paid_server(world):
    world["cfg"]["ultra_card_json"] = json.dumps({"text_color": "gold"})
    asyncio.run(cc._save_option(1, None, 42, text_color="pink"))
    asyncio.run(cc._save_option(1, None, 42, font="gothic"))
    it = press(cc.CardUndoButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["font"] == parse_ultra_options(None)["font"]
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["text_color"] == "pink"
    press(cc.CardUndoButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["text_color"] == "gold"
    assert world["rerenders"] == 2 and not it.sent


def test_undo_also_takes_back_the_avatar_shape_and_presets(world):
    asyncio.run(cc._apply_style(1, None, 42, {"banner": "top"}, "hexagon"))
    assert world["cfg"]["avatar_shape"] == "hexagon"
    press(cc.CardUndoButton(1, None, 42))
    assert world["cfg"]["avatar_shape"] == "circle" and parse_ultra_options(world["cfg"]["ultra_card_json"])["banner"] == "bottom"


def test_undo_with_nothing_to_undo_says_so_and_writes_nothing(world):
    it = press(cc.CardUndoButton(1, None, 42))
    assert world["writes"] == [] and world["rerenders"] == 0
    assert it.sent and "Nothing to undo" in it.sent[0][0][0] and it.sent[0][1]["ephemeral"] is True


def test_undo_is_per_person_and_capped(world):
    for i in range(15):
        asyncio.run(cc._save_option(1, None, 42, heading=f"h{i}"))
    assert len(cc._UNDO[KEY]) == cc._UNDO_MAX
    press(cc.CardUndoButton(1, None, 99), uid=99)              # someone else's undo does not touch mine
    assert len(cc._UNDO[KEY]) == cc._UNDO_MAX


def test_undo_on_an_unpaid_server_only_touches_the_draft(world):
    world["cfg"] = {"ultra_pack_unlocked": False}
    asyncio.run(cc._save_option(1, None, 42, text_color="red"))
    asyncio.run(cc._save_option(1, None, 42, text_color="green"))
    press(cc.CardUndoButton(1, None, 42))
    assert world["writes"] == [] and cc._get_draft(1, None, 42)["opts"]["text_color"] == "red"


def test_undo_without_access_changes_nothing(world):
    asyncio.run(cc._save_option(1, None, 42, text_color="red"))
    n = len(world["writes"])
    world["access"] = False
    it = press(cc.CardUndoButton(1, None, 42))
    assert len(world["writes"]) == n and it.deferred == [] and len(cc._UNDO[KEY]) == 1


# ---------- Reset this step ----------

def _set_tab(tab):
    cc._set_tab(KEY, tab)


def test_reset_this_step_only_resets_that_step_and_keeps_picture_and_heading(world):
    world["cfg"].update(custom_background_url="https://x/y.png", avatar_shape="hexagon", ultra_card_json=json.dumps(
        {"banner": "top", "avatar_side": "right", "layout": "centered", "text_color": "gold", "font": "gothic", "ratio": "16:9",
         "heading": "Hi", "shadow": True}))
    _set_tab("layout")
    press(cc.CardStepResetButton(1, None, 42))
    o = parse_ultra_options(world["cfg"]["ultra_card_json"])
    d = parse_ultra_options(None)
    assert (o["banner"], o["avatar_side"], o["layout"]) == (d["banner"], d["avatar_side"], d["layout"])
    assert o["text_color"] == "gold" and o["font"] == "gothic" and o["ratio"] == "16:9" and o["heading"] == "Hi"
    assert world["cfg"]["avatar_shape"] == "circle" and world["cfg"]["custom_background_url"] == "https://x/y.png"
    _set_tab("text")
    press(cc.CardStepResetButton(1, None, 42))
    o = parse_ultra_options(world["cfg"]["ultra_card_json"])
    assert (o["text_color"], o["font"], o["heading"], o["shadow"]) == (d["text_color"], d["font"], d["heading"], d["shadow"])
    assert o["ratio"] == "16:9"
    _set_tab("bg")
    press(cc.CardStepResetButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["ratio"] == d["ratio"]


def test_reset_this_step_can_be_undone(world):
    world["cfg"]["ultra_card_json"] = json.dumps({"text_color": "gold"})
    _set_tab("text")
    press(cc.CardStepResetButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["text_color"] == parse_ultra_options(None)["text_color"]
    press(cc.CardUndoButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["text_color"] == "gold"


def test_every_reset_field_is_a_real_option():
    valid = set(parse_ultra_options(None))
    for step, fields in cc._STEP_RESET_FIELDS.items():
        assert set(fields) <= valid, step
    assert set(cc._STEP_RESET_FIELDS) == {"bg", "layout", "text"}


def test_reset_all_on_the_send_step_still_resets_everything_and_is_undoable(world):
    world["cfg"]["ultra_card_json"] = json.dumps({"font": "gothic", "ratio": "16:9"})
    press(cc.CardResetButton(1, None, 42))
    assert world["cfg"]["ultra_card_json"] is None
    press(cc.CardUndoButton(1, None, 42))
    assert parse_ultra_options(world["cfg"]["ultra_card_json"])["font"] == "gothic"


# ---------- More button / steps ----------

def test_more_button_toggles_only_the_current_step_and_does_not_redraw_the_card(world, monkeypatch):
    shown = []

    async def fake_show(interaction, g, c, inv, tab):
        shown.append(tab)
    monkeypatch.setattr(cc, "_show_step", fake_show)
    _set_tab("text")
    press(cc.CardMoreButton(1, None, 42))
    assert shown == ["text"] and cc._more_open(KEY, "text") and not cc._more_open(KEY, "layout")
    assert world["rerenders"] == 0 and world["renders"] == []
    press(cc.CardMoreButton(1, None, 42))
    assert not cc._more_open(KEY, "text")


def test_show_step_builds_the_step_with_its_more_state(world):
    it = It()
    it.message = None

    async def edit(**kw):
        it.edits.append(kw)
    it.edit_original_response = edit
    cc._TV_STATE.clear()
    cc._MORE_STATE.clear()
    cc._toggle_more(KEY, "layout")
    asyncio.run(cc._show_step(it, 1, None, 42, "layout"))
    asyncio.run(cc._show_step(it, 1, None, 42, "text"))
    assert "shape" in kinds(it.edits[0]["view"]) and "bracket" not in kinds(it.edits[1]["view"])


# ---------- Send me a test ----------

def test_send_test_posts_the_full_size_card_privately(world):
    it = press(cc.CardTestSendButton(1, None, 42))
    assert world["renders"] == [True]
    assert it.deferred[0].get("ephemeral") is True
    (_a, kw), = it.sent
    assert kw["ephemeral"] is True and kw["file"].filename == "test-card.png" and "unlock" not in kw["content"].lower()


def test_send_test_on_an_unpaid_server_mentions_the_watermark(world):
    world["cfg"] = {"ultra_pack_unlocked": False}
    it = press(cc.CardTestSendButton(1, None, 42))
    assert "unlock" in it.sent[0][1]["content"].lower()


def test_send_test_has_a_cooldown_and_needs_access(world):
    press(cc.CardTestSendButton(1, None, 42))
    it = press(cc.CardTestSendButton(1, None, 42))
    assert world["renders"] == [True] and it.said and it.said[0][1]["ephemeral"] is True
    cc._TEST_LAST.clear()
    world["access"] = False
    it = press(cc.CardTestSendButton(1, None, 42))
    assert world["renders"] == [True] and it.deferred == [] and not it.sent


def test_send_test_failure_is_a_friendly_message_not_a_crash(world, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(cc, "_render_tv", boom)
    it = press(cc.CardTestSendButton(1, None, 42))
    assert "Couldn't draw" in it.sent[0][0][0] and "nope" not in it.sent[0][0][0]


def test_full_size_render_is_not_downscaled_but_the_tv_is():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (1536, 1024), "blue").save(buf, format="PNG")
    tv = Image.open(io.BytesIO(cc._tv_finish(buf.getvalue(), False))).width
    full = Image.open(io.BytesIO(cc._tv_finish(buf.getvalue(), False, True))).width
    assert tv == cc._TV_WIDTH and full == 1536


# ---------- nothing about the card itself changed ----------

def test_the_card_options_and_defaults_are_untouched():
    assert parse_ultra_options(None) == parse_ultra_options("{}") == parse_ultra_options("not json")
    assert set(cc._STEP_RESET_FIELDS["text"]) >= set(cc._STYLE_EXTRAS)
