"""Owner-panel Hosting wizard: pick a channel, choose cats / welcome / both, test, and keep old cats safe."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, OTHER = 111, 222
MAIN = "discord_bot.cogs._views_admin_panel"
SYS = "discord_bot.cogs._views_admin_panel_system"
HW = "discord_bot.cogs._views_admin_panel_hosting"


@pytest.fixture()
def hw(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.IMAGE_HOST_CHANNEL_ID = 5
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for m in (MAIN, SYS, HW):
        sys.modules.pop(m, None)
    mod = importlib.import_module(HW)
    mod.main = importlib.import_module(MAIN)
    yield mod
    for m in (MAIN, SYS, HW):
        sys.modules.pop(m, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = None
    i.data = {"values": values or []}
    i.message.id = 555
    done = {"v": False}

    async def mark(*a, **k):
        done["v"] = True
    for n in ("send_message", "edit_message", "defer"):
        setattr(i.response, n, AsyncMock(side_effect=mark))       # once the response is used, is_done() is True
    i.response.is_done = MagicMock(side_effect=lambda: done["v"])
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


class FakeHost:
    """Stands in for modules.image_host: settings live in a dict."""
    def __init__(self, ih, monkeypatch, host=0, cat=0, saved=None, channel_ok=True, problem=None):
        self.settings = {ih.HOST_SETTING: host, ih.CAT_SETTING: cat}
        self.moved = []

        async def setting_id(key): return self.settings.get(key, 0)
        async def set_setting(key, cid): self.settings[key] = int(cid) if cid else 0
        async def saved_cat_pictures(limit=6): return saved or []
        async def open_channel(bot, cid):
            return (MagicMock(id=cid), None) if channel_ok else (None, "this bot has no access to that channel")
        async def auto_move(bot): self.moved.append(1)
        monkeypatch.setattr(ih, "setting_id", setting_id)
        monkeypatch.setattr(ih, "set_setting", set_setting)
        monkeypatch.setattr(ih, "saved_cat_pictures", saved_cat_pictures)
        monkeypatch.setattr(ih, "open_channel", open_channel)
        monkeypatch.setattr(ih, "permission_problem", lambda ch, bot: problem)
        monkeypatch.setattr(ih, "auto_move", auto_move)


def make(hw, owner=OWNER, **kw):
    return run(hw.HostingWizardView.create(MagicMock(), owner, **kw))


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def test_describe_state_default_and_own_channels(hw):
    lines = "\n".join(hw.describe_state({"host_id": 0, "default_id": 5, "cat_id": 0, "saved": []}))
    assert "<#5>" in lines and "default" in lines and "follow the welcome channel" in lines and "No cat pictures" in lines
    lines = "\n".join(hw.describe_state({"host_id": 7, "default_id": 5, "cat_id": 9, "saved": [(5, 1200, 300), (7, 1, 0)]}))
    assert "<#9>" in lines and "1,200 pictures" in lines and "300 adopted" in lines and "1 picture," in lines


def test_the_warning_says_old_cats_are_safe(hw):
    w = hw.change_warning()
    assert "never deletes or moves" in w and "keep loading" in w and "Don't delete the old channel" in w


def test_wizard_builds_within_discord_limits_and_starts_with_actions_off(hw, monkeypatch):
    from modules import image_host as ih
    FakeHost(ih, monkeypatch)
    v = make(hw)
    assert v.to_components()
    b = buttons(v)
    assert all(b[n].disabled for n in ("Use for cat images", "Use for welcome backgrounds", "Use for both"))
    assert b["Cats follow the welcome channel"].disabled
    assert any(isinstance(c, discord.ui.ChannelSelect) for c in v.walk_children())


def test_picking_a_good_channel_enables_the_use_buttons(hw, monkeypatch):
    from modules import image_host as ih
    FakeHost(ih, monkeypatch)
    v = make(hw)
    i = I(values=["900"])
    run(v._pick(i))
    shown = i.followup.edit_message.call_args.kwargs["view"]
    b = buttons(shown)
    assert not b["Use for cat images"].disabled and "can store pictures" in text_of(shown)


def test_picking_an_unreachable_or_underpowered_channel_is_refused(hw, monkeypatch):
    from modules import image_host as ih
    FakeHost(ih, monkeypatch, channel_ok=False)
    v = make(hw)
    i = I(values=["900"])
    run(v._pick(i))
    shown = i.followup.edit_message.call_args.kwargs["view"]
    assert buttons(shown)["Use for cat images"].disabled and "no access" in text_of(shown)
    FakeHost(ih, monkeypatch, problem="this bot is missing: Attach Files")
    i = I(values=["900"])
    run(make(hw)._pick(i))
    shown = i.followup.edit_message.call_args.kwargs["view"]
    assert buttons(shown)["Use for cat images"].disabled and "Attach Files" in text_of(shown)


def test_cat_choice_needs_a_second_press_and_only_changes_the_cat_channel(hw, monkeypatch):
    from modules import image_host as ih
    fake = FakeHost(ih, monkeypatch, host=7)
    v = hw.HostingWizardView(MagicMock(), OWNER, {"host_id": 7, "default_id": 5, "cat_id": 0, "saved": []}, picked=900, picked_ok=True)
    i = I()
    run(v._use("cats")(i))                                    # first press: ask to confirm, change nothing
    assert fake.settings[ih.CAT_SETTING] == 0
    confirm_view = i.followup.edit_message.call_args.kwargs["view"]
    assert "Confirm" in buttons(confirm_view) and "Press Confirm" in text_of(confirm_view)
    i2 = I()
    run(confirm_view._use("cats")(i2))                        # second press: applies
    assert fake.settings[ih.CAT_SETTING] == 900 and fake.settings[ih.HOST_SETTING] == 7
    assert fake.moved == []                                   # welcome backgrounds were not touched
    done = i2.followup.edit_message.call_args.kwargs["view"]
    assert "stay where they are" in text_of(done)


def test_welcome_and_both_choices_save_and_start_the_background_move(hw, monkeypatch):
    from modules import image_host as ih
    fake = FakeHost(ih, monkeypatch)
    snap = {"host_id": 0, "default_id": 5, "cat_id": 0, "saved": []}

    async def go():
        v = hw.HostingWizardView(MagicMock(), OWNER, snap, picked=900, picked_ok=True, confirm="both")
        await v._use("both")(I())
        await asyncio.sleep(0)
    run(go())
    assert fake.settings[ih.HOST_SETTING] == 900 and fake.settings[ih.CAT_SETTING] == 900 and fake.moved == [1]


def test_cats_can_go_back_to_following_the_welcome_channel(hw, monkeypatch):
    from modules import image_host as ih
    fake = FakeHost(ih, monkeypatch, host=7, cat=900)
    v = make(hw)
    assert not buttons(v)["Cats follow the welcome channel"].disabled
    i = I()
    run(v._reset_cats(i))
    assert fake.settings[ih.CAT_SETTING] == 0 and fake.settings[ih.HOST_SETTING] == 7


def test_a_failed_save_changes_nothing_and_says_so(hw, monkeypatch):
    from modules import image_host as ih
    FakeHost(ih, monkeypatch)

    async def boom(key, cid): raise RuntimeError("db down")
    monkeypatch.setattr(ih, "set_setting", boom)
    v = hw.HostingWizardView(MagicMock(), OWNER, {"host_id": 0, "default_id": 5, "cat_id": 0, "saved": []},
                             picked=900, picked_ok=True, confirm="cats")
    i = I()
    run(v._use("cats")(i))
    assert "Nothing was changed" in text_of(i.followup.edit_message.call_args.kwargs["view"])


def test_only_owners_can_use_it(hw):
    assert hw.allowed(OWNER) and not hw.allowed(OTHER)
    v = make(hw, owner=OWNER)
    assert run(v.interaction_check(I(user=OTHER))) is False


# ── image_host: the cat channel ──────────────────────────────────────────

def test_cat_candidates_put_the_cat_channel_first_then_the_normal_ones(monkeypatch):
    ih = importlib.import_module("modules.image_host")

    async def setting_id(key): return {ih.CAT_SETTING: 9, ih.HOST_SETTING: 7}.get(key, 0)
    async def candidates(): return [(7, "owner"), (5, "default")]
    monkeypatch.setattr(ih, "setting_id", setting_id)
    monkeypatch.setattr(ih, "candidates", candidates)
    assert [c for c, _ in run(ih.cat_candidates())] == [9, 7, 5]

    async def none(key): return 0
    monkeypatch.setattr(ih, "setting_id", none)
    assert [c for c, _ in run(ih.cat_candidates())] == [7, 5]


def test_cat_channel_falls_back_when_the_bot_cannot_reach_it(monkeypatch):
    ih = importlib.import_module("modules.image_host")

    async def cats(): return [(9, "cat"), (5, "default")]
    async def open_channel(bot, cid):
        return (None, "no access") if cid == 9 else (MagicMock(id=cid), None)
    monkeypatch.setattr(ih, "cat_candidates", cats)
    monkeypatch.setattr(ih, "open_channel", open_channel)
    assert run(ih.resolve_cat_channel(MagicMock())).id == 5


def test_permission_problem_lists_what_is_missing(monkeypatch):
    ih = importlib.import_module("modules.image_host")
    perms = discord.Permissions(view_channel=True, send_messages=True, attach_files=False, read_message_history=False)
    ch = MagicMock()
    ch.permissions_for.return_value = perms
    monkeypatch.setattr(ih, "_me", lambda c, b: MagicMock())
    text = ih.permission_problem(ch, MagicMock())
    assert "Attach Files" in text and "Read Message History" in text and "Send Messages" not in text
    ch.permissions_for.return_value = discord.Permissions.all()
    assert ih.permission_problem(ch, MagicMock()) is None
