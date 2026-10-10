"""Owner-panel Phase 4 tests: Bump, Feedback and System (real discord.py components, stubbed DB/config)."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, OTHER = 111, 222
MAIN = "discord_bot.cogs._views_admin_panel"
MOD = "discord_bot.cogs._views_admin_panel_system"


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(MAIN, None)
    sys.modules.pop(MOD, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(MOD)
    mod.main = main
    yield mod
    sys.modules.pop(MAIN, None)
    sys.modules.pop(MOD, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None, channel_id=900):
    i = MagicMock()
    i.user.id = user
    i.guild_id = None
    i.channel_id = channel_id
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def cog():
    c = MagicMock()
    for n in ("bumpadmin_cooldown", "bumpadmin_list", "bumpadmin_review", "bumpadmin_cleanup_reminders"):
        setattr(c.bump, n, AsyncMock())
    c.feedback_cog.viewfeedback = AsyncMock()
    for n in ("submissions", "revenue", "envcheck", "exportusers", "stats", "pending", "aidebug"):
        setattr(c.admin_cog, n, AsyncMock())
    c.welcome.hostingchannel = AsyncMock()
    return c


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


# ── access / navigation ──────────────────────────────────────────────────

def test_sections_follow_the_same_allowlists_as_the_commands(vp):
    a = vp.main.allowed_sections(OWNER)
    assert {"bump", "feedback", "system"} <= a
    assert not ({"bump", "feedback", "system"} & vp.main.allowed_sections(OTHER))


def test_feedback_uses_broadcast_list_and_bump_system_use_clone_admin_list(vp, monkeypatch):
    monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", set())
    a = vp.main.allowed_sections(OWNER)
    assert "feedback" not in a and {"bump", "system"} <= a
    monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
    monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", {OWNER})
    a = vp.main.allowed_sections(OWNER)
    assert "feedback" in a and not ({"bump", "system"} & a)


def test_home_has_phase4_buttons_and_they_are_gated(vp, monkeypatch):
    async def go():
        b = buttons(vp.main.HomeView(cog(), OWNER))
        assert not any(b[n].disabled for n in ("Bump", "Feedback", "System"))
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", set())
        b = buttons(vp.main.HomeView(cog(), OWNER))
        assert all(b[n].disabled for n in ("Bump", "Feedback", "System"))
    run(go())


@pytest.mark.parametrize("label,cls", [("Bump", "BumpHubView"), ("Feedback", "FeedbackView"), ("System", "SystemView")])
def test_home_to_screen_and_back(vp, label, cls):
    async def go():
        c = cog(); home = vp.main.HomeView(c, OWNER); i = I()
        await buttons(home)[label].callback(i)
        v = i.response.edit_message.await_args.kwargs["view"]
        assert isinstance(v, getattr(vp, cls))
        i2 = I()
        await buttons(v)["Back"].callback(i2)
        assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.main.HomeView)
    run(go())


def test_views_block_non_opener_and_revoked(vp, monkeypatch):
    async def go():
        for v in (vp.BumpHubView(cog(), OWNER), vp.SystemView(cog(), OWNER), vp.FeedbackView(cog(), OWNER)):
            assert await v.interaction_check(I(user=OTHER)) is False
        v = vp.BumpHubView(cog(), OWNER)
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", set())
        assert await v.interaction_check(I()) is False
    run(go())


# ── bump ─────────────────────────────────────────────────────────────────

def test_bump_read_only_buttons_call_the_existing_commands(vp):
    async def go():
        c = cog(); v = vp.BumpHubView(c, OWNER)
        b = buttons(v)
        for label, name in (("View cooldown", "bumpadmin_cooldown"), ("Configured servers", "bumpadmin_list"),
                            ("Review queue", "bumpadmin_review")):
            i = I()
            await b[label].callback(i)
            getattr(c.bump, name).assert_awaited_once_with(i)
    run(go())


def test_bump_set_cooldown_opens_modal(vp):
    async def go():
        v = vp.BumpHubView(cog(), OWNER); i = I()
        await buttons(v)["Set cooldown"].callback(i)
        assert isinstance(i.response.send_modal.await_args.args[0], vp.CooldownModal)
    run(go())


def test_cooldown_modal_validates_and_calls_command(vp):
    async def go():
        c = cog()
        for bad in ("", "abc", "0", "-5", "1.5"):
            m = vp.CooldownModal(c); m.minutes._value = bad; i = I()
            await m.on_submit(i)
            i.response.send_message.assert_awaited_once()
            c.bump.bumpadmin_cooldown.assert_not_awaited()
        m = vp.CooldownModal(c); m.minutes._value = " 90 "; i = I()
        await m.on_submit(i)
        c.bump.bumpadmin_cooldown.assert_awaited_once_with(i, minutes=90)
    run(go())


def test_cooldown_modal_rechecks_authorization(vp, monkeypatch):
    async def go():
        c = cog(); m = vp.CooldownModal(c); m.minutes._value = "60"; i = I(user=OTHER)
        await m.on_submit(i)
        c.bump.bumpadmin_cooldown.assert_not_awaited()
        assert "no longer authorized" in i.response.send_message.await_args.args[0]
    run(go())


def test_bump_cleanup_is_two_step(vp):
    async def go():
        c = cog(); v = vp.BumpHubView(c, OWNER); i = I()
        await buttons(v)["Clean up reminders"].callback(i)
        c.bump.bumpadmin_cleanup_reminders.assert_not_awaited()
        assert v._confirm is True
        i2 = I()
        await buttons(v)["Confirm cleanup"].callback(i2)
        c.bump.bumpadmin_cleanup_reminders.assert_awaited_once_with(i2)
        assert v._confirm is False
    run(go())


def test_bump_missing_module_is_reported_not_crashed(vp):
    async def go():
        c = cog(); c.bump = None; v = vp.BumpHubView(c, OWNER); i = I()
        await buttons(v)["Review queue"].callback(i)
        assert "isn't loaded" in i.response.send_message.await_args.args[0]
    run(go())


# ── feedback ─────────────────────────────────────────────────────────────

def test_feedback_limit_select_and_view(vp):
    async def go():
        c = cog(); v = vp.FeedbackView(c, OWNER)
        assert v.limit == 10
        sel = selects(v)[0]
        assert {o.value for o in sel.options} == {"5", "10", "25"}
        i = I(values=["25"])
        await sel.callback(i)
        assert v.limit == 25
        i2 = I()
        await buttons(v)["View feedback"].callback(i2)
        c.feedback_cog.viewfeedback.assert_awaited_once_with(i2, limit=25)
    run(go())


# ── system ───────────────────────────────────────────────────────────────

def test_system_read_only_buttons(vp):
    async def go():
        c = cog(); v = vp.SystemView(c, OWNER)
        for label, name in (("Submissions", "submissions"), ("Revenue", "revenue"), ("Env check", "envcheck")):
            i = I()
            await buttons(v)[label].callback(i)
            getattr(c.admin_cog, name).assert_awaited_once_with(i)
    run(go())


def test_system_export_is_two_step(vp):
    async def go():
        c = cog(); v = vp.SystemView(c, OWNER); i = I()
        await buttons(v)["Export users"].callback(i)
        c.admin_cog.exportusers.assert_not_awaited()
        i2 = I()
        await buttons(v)["Confirm export"].callback(i2)
        c.admin_cog.exportusers.assert_awaited_once_with(i2)
        assert v._confirm is None
    run(go())


def test_system_hosting_channel_is_two_step_and_gated(vp, monkeypatch):
    async def go():
        c = cog(); v = vp.SystemView(c, OWNER); i = I()
        await buttons(v)["Set hosting channel"].callback(i)
        c.welcome.hostingchannel.assert_not_awaited()
        i2 = I()
        await buttons(v)["Confirm hosting channel"].callback(i2)
        c.welcome.hostingchannel.assert_awaited_once_with(i2)
        # not on the broadcast/owner list -> button disabled
        monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", {999})
        assert buttons(vp.SystemView(cog(), OWNER))["Set hosting channel"].disabled
    run(go())


def test_pending_confirm_switches_between_the_two_step_actions(vp):
    async def go():
        c = cog(); v = vp.SystemView(c, OWNER)
        await buttons(v)["Set hosting channel"].callback(I())
        await buttons(v)["Export users"].callback(I())   # switching target must NOT fire the export
        c.admin_cog.exportusers.assert_not_awaited()
        assert v._confirm == "export"
    run(go())


def test_screens_render_within_discord_limits(vp):
    def count(view):
        return sum(1 for _ in view.walk_children())
    for v in (vp.BumpHubView(cog(), OWNER), vp.FeedbackView(cog(), OWNER), vp.SystemView(cog(), OWNER)):
        assert count(v) < 40
        assert len(buttons(v)) <= 15          # Discord allows 25 buttons in a panel; stay well under


# ── quick wins: stats / pending checkouts / AI debug ─────────────────────

@pytest.mark.parametrize("label,cmd", [("Stats", "stats"), ("Pending checkouts", "pending"), ("AI debug", "aidebug")])
def test_system_quick_wins_call_the_slash_command_code(vp, label, cmd):
    async def go():
        c = cog(); v = vp.SystemView(c, OWNER); i = I()
        await buttons(v)[label].callback(i)
        getattr(c.admin_cog, cmd).assert_awaited_once_with(i)
    run(go())


def test_system_quick_wins_report_missing_module(vp):
    async def go():
        c = cog(); c.admin_cog = None; v = vp.SystemView(c, OWNER)
        for label in ("Stats", "Pending checkouts", "AI debug"):
            i = I(); await buttons(v)[label].callback(i)
            i.response.send_message.assert_awaited_once()
    run(go())


def test_system_view_still_within_discord_limits_with_new_buttons(vp):
    v = vp.SystemView(cog(), OWNER)
    assert len(buttons(v)) == 11
    assert v.to_components()


# ── Test image hosting ───────────────────────────────────────────────────

def test_system_view_has_the_image_hosting_test_button(vp):
    v = vp.SystemView(cog(), OWNER)
    assert "Test image hosting" in buttons(v) and buttons(v)["Test image hosting"].disabled is False


def test_image_hosting_test_runs_for_this_bot_and_shows_the_report_and_card(vp, monkeypatch):
    from modules import image_host as ih
    seen = {}

    async def diagnose(bot, **kw):
        seen["bot"] = bot
        return [ih.Check("Channel id", True, "`1`"), ih.Check("Upload", False, "Discord refused the upload (Forbidden)", "allow Attach Files")], b"PNG"
    monkeypatch.setattr(ih, "diagnose", diagnose)
    v = vp.SystemView(cog(), OWNER)
    i = I()
    run(buttons(v)["Test image hosting"].callback(i))
    i.response.defer.assert_awaited_once()
    assert seen["bot"] is i.client
    text = i.followup.send.await_args.args[0]
    assert text.startswith("❌") and "Fix: allow Attach Files" in text
    kw = i.followup.send.await_args.kwargs
    assert kw["ephemeral"] is True and [f.filename for f in kw["files"]] == ["test-welcome-card.png"]


def test_image_hosting_test_says_so_when_it_crashes(vp, monkeypatch):
    from modules import image_host as ih

    async def boom(bot, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(ih, "diagnose", boom)
    i = I()
    run(buttons(vp.SystemView(cog(), OWNER))["Test image hosting"].callback(i))
    assert "crashed" in i.followup.send.await_args.args[0] and i.followup.send.await_args.kwargs["ephemeral"] is True


def test_only_the_panel_owner_can_press_it(vp):
    v = vp.SystemView(cog(), OWNER)
    i = I(user=OTHER)
    i.response.send_message = AsyncMock()
    assert run(v.interaction_check(i)) is False


def test_move_saved_backgrounds_button_runs_the_mover_for_this_bot(vp, monkeypatch):
    from modules import image_host as ih
    seen = {}

    async def move(bot, **kw):
        seen["bot"] = bot
        return ih.MoveResult(checked=3, already_ok=1, moved=1, failed=[(55, "its hosting message is gone or unreachable")])
    monkeypatch.setattr(ih, "move_backgrounds", move)
    i = I()
    run(buttons(vp.SystemView(cog(), OWNER))["Move saved backgrounds"].callback(i))
    i.response.defer.assert_awaited_once()
    text = i.followup.send.await_args.args[0]
    assert seen["bot"] is i.client and "Moved now: **1**" in text and "`55`" in text and i.followup.send.await_args.kwargs["ephemeral"] is True


def test_move_saved_backgrounds_button_says_so_when_it_crashes(vp, monkeypatch):
    from modules import image_host as ih

    async def boom(bot, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(ih, "move_backgrounds", boom)
    i = I()
    run(buttons(vp.SystemView(cog(), OWNER))["Move saved backgrounds"].callback(i))
    assert "crashed" in i.followup.send.await_args.args[0]
