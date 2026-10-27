"""Verification by DM (free) + the V2, restart-proof setup opened from the join DM's More features page."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import _views_verification as vv
from discord_bot.cogs import _views_verify_setup as vs


def _inner(view):
    return [getattr(c, "item", c) for c in view.walk_children()]


def _labels(view):
    return [c.label for c in _inner(view) if isinstance(c, discord.ui.Button)]


class FakeDB:
    def __init__(self, **cfg):
        self.cfg = {"guild_id": 5, "clone_id": None, "mode": "captcha", "channel_id": None, "unverified_role_id": None,
                    "verified_role_id": None, "enabled": False, "dm_verify": False, "message_id": None,
                    **{k: None for k in ("panel_title", "panel_text", "panel_color", "panel_image_url", "log_channel_id",
                                          "kick_after_minutes", "min_account_age_days", "welcome_text")}, **cfg}
        self.calls = []

    async def get_verification_config(self, guild_id, clone_id=None):
        return dict(self.cfg)

    async def set_verification_config(self, guild_id, clone_id=None, **fields):
        self.calls.append(fields)
        self.cfg.update(fields)
        return dict(self.cfg)


@pytest.fixture()
def fake(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(vs, "db", d)
    monkeypatch.setattr(vv, "db", d)
    monkeypatch.setattr(vs, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(vs.verification_pro, "is_premium", AsyncMock(return_value=False))
    return d


def _interaction(client=None):
    i = MagicMock()
    i.client = client or MagicMock()
    i.guild = None
    i.guild_id = None
    i.response.defer = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.edit_original_response = AsyncMock()
    i.followup.send = AsyncMock()
    i.user.id = 77
    return i


# ── the screen ───────────────────────────────────────────────────────────

def test_setup_page_has_dm_option_and_premium_section(fake):
    import asyncio
    view = asyncio.run(vs.build_view(MagicMock(), 5, None))
    labels = _labels(view)
    assert "DM: Off" in labels and "Lock down & enable" in labels and "Open" in labels      # Open = Premium extras
    assert len(list(view.walk_children())) <= 40
    view.to_components()


def test_enable_button_is_disabled_until_channel_and_role_are_set(fake):
    import asyncio
    view = asyncio.run(vs.build_view(MagicMock(), 5, None))
    enable = [c for c in _inner(view) if isinstance(c, discord.ui.Button) and c.label == "Lock down & enable"][0]
    assert enable.disabled
    fake.cfg.update(channel_id=1, unverified_role_id=2)
    view = asyncio.run(vs.build_view(MagicMock(), 5, None))
    enable = [c for c in _inner(view) if isinstance(c, discord.ui.Button) and c.label == "Lock down & enable"][0]
    assert not enable.disabled


def test_live_setup_shows_turn_off_and_dm_state(fake):
    import asyncio
    fake.cfg.update(channel_id=1, unverified_role_id=2, enabled=True, dm_verify=True)
    labels = _labels(asyncio.run(vs.build_view(MagicMock(), 5, None)))
    assert "Turn off" in labels and "DM: On" in labels


def test_premium_page_is_there_for_free_servers_and_says_so(fake):
    import asyncio
    view = asyncio.run(vs.build_view(MagicMock(), 5, None, page="pro"))
    assert "Get Premium" in _labels(view) and "Back to setup" in _labels(view)
    assert len(list(view.walk_children())) <= 40
    view.to_components()
    assert "Needs Premium" in " ".join(getattr(c, "content", "") for c in view.walk_children())


# ── restart survival ─────────────────────────────────────────────────────

def test_every_control_is_a_registered_dynamic_item_that_round_trips(fake):
    import asyncio
    for page in ("setup", "pro"):
        fake.cfg.update(channel_id=1, unverified_role_id=2, enabled=True)
        view = asyncio.run(vs.build_view(MagicMock(), 5, 3, page=page))
        for item in view.walk_children():
            if not isinstance(item, discord.ui.DynamicItem):
                continue
            if type(item).__module__ == "modules.channel_finder":      # the global "Find by name" helper, added when installed
                continue
            cid = item.item.custom_id
            assert len(cid) <= 100
            assert type(item) in vs.DYNAMIC_ITEMS, type(item)
            assert type(item).__discord_ui_compiled_template__.match(cid), cid
    for cls in vs.DYNAMIC_ITEMS:
        assert cls in jd.DYNAMIC_ITEMS
    assert jd._VerificationButton in jd.DYNAMIC_ITEMS and vv.VerifyDmButton in vv.DYNAMIC_ITEMS


def test_items_are_rebuilt_from_a_custom_id(fake):
    import asyncio
    for cls, cid in ((vs.DmToggleButton, "vfysetup_dm:5:-"), (vs.EnableButton, "vfysetup_go:5:3"),
                     (vs.ModeSelect, "vfysetup_mode:5:-"), (vs.UnverifiedPick, "vfysetup_unv:5:3"),
                     (vs.ChoicePick, "vfysetup_kick:5:3"), (vs.ChoicePick, "vfysetup_age:5:-")):
        m = cls.__discord_ui_compiled_template__.match(cid)
        assert m, cid
        item = asyncio.run(cls.from_custom_id(MagicMock(), MagicMock(), m))
        assert item.item.custom_id == cid


# ── callbacks ────────────────────────────────────────────────────────────

def test_dm_toggle_saves_and_redraws(fake):
    import asyncio
    i = _interaction()
    asyncio.run(vs.DmToggleButton(5, None, False).callback(i))
    assert fake.cfg["dm_verify"] is True
    i.edit_original_response.assert_awaited()
    asyncio.run(vs.DmToggleButton(5, None, True).callback(i))
    assert fake.cfg["dm_verify"] is False


def test_mode_select_saves(fake, monkeypatch):
    import asyncio
    monkeypatch.setattr(discord.ui.Select, "values", property(lambda self: ["button"]))
    asyncio.run(vs.ModeSelect(5, None, "captcha").callback(_interaction()))
    assert fake.cfg["mode"] == "button"


def test_a_user_without_manage_server_is_stopped(fake, monkeypatch):
    import asyncio
    monkeypatch.setattr(vs, "_check_access", AsyncMock(return_value=False))
    i = _interaction()
    asyncio.run(vs.DmToggleButton(5, None, False).callback(i))
    assert fake.cfg["dm_verify"] is False and not fake.calls
    i.edit_original_response.assert_not_called()


def test_off_button_turns_verification_off(fake):
    import asyncio
    fake.cfg.update(enabled=True)
    asyncio.run(vs.OffButton(5, None).callback(_interaction()))
    assert fake.cfg["enabled"] is False


def test_enable_refuses_until_channel_and_role_are_picked(fake):
    import asyncio
    guild = MagicMock(); guild.id = 5
    assert "Still needed" in asyncio.run(vs.enable_verification(guild, None))


def test_premium_look_is_saved_through_the_modal(fake):
    import asyncio
    modal = vs.LookModal(5, None, fake.cfg)
    modal.title_in._value = "Hi"; modal.text_in._value = ""; modal.color_in._value = "#112233"
    modal.image_in._value = ""; modal.welcome_in._value = "Welcome {user}"
    i = _interaction()
    asyncio.run(modal.on_submit(i))
    assert fake.cfg["panel_title"] == "Hi" and fake.cfg["panel_color"] == 0x112233 and fake.cfg["welcome_text"] == "Welcome {user}"
    bad = vs.LookModal(5, None, fake.cfg)
    bad.title_in._value = ""; bad.text_in._value = ""; bad.color_in._value = "red"; bad.image_in._value = ""; bad.welcome_in._value = ""
    i2 = _interaction()
    asyncio.run(bad.on_submit(i2))
    i2.response.send_message.assert_awaited()
    assert fake.cfg["panel_color"] == 0x112233                       # nothing saved on a bad value


# ── the More features page ───────────────────────────────────────────────

def test_more_features_has_the_verification_section():
    view = jd.build_join_dm_more_view(5, None)
    assert "Set up" in _labels(view)
    text = " ".join(getattr(c, "content", "") for c in view.walk_children())
    assert "Verification" in text and "Free" in text
    assert len(list(view.walk_children())) <= 40
    view.to_components()
    verify = [c for c in _inner(view) if isinstance(c, discord.ui.Button) and (c.custom_id or "").startswith("join_dm_verify:")]
    assert verify and jd._VerificationButton.__discord_ui_compiled_template__.match(verify[0].custom_id)


# ── the DM itself ────────────────────────────────────────────────────────

def _guild(gid=5, name="Cool Server"):
    g = MagicMock(); g.id = gid; g.name = name
    return g


def test_dm_prompt_is_v2_with_a_persistent_button_and_uses_the_premium_look():
    plain = vv.build_dm_prompt(_guild(), "button")
    plain.to_components()
    assert "Verify me" in _labels(plain)
    look = {"panel_title": "Hello there", "panel_text": "Press it.", "panel_color": 0x123456, "panel_image_url": "https://x.test/a.png"}
    fancy = vv.build_dm_prompt(_guild(), "captcha", look)
    fancy.to_components()
    text = " ".join(getattr(c, "content", "") for c in fancy.walk_children())
    assert "Hello there" in text and "Press it." in text
    assert any(isinstance(c, discord.ui.MediaGallery) for c in fancy.walk_children())
    btn = [c for c in _inner(plain) if isinstance(c, discord.ui.Button)][0]
    assert vv.VerifyDmButton.__discord_ui_compiled_template__.match(btn.custom_id)


def test_send_verify_dm_reports_closed_dms(monkeypatch):
    import asyncio
    monkeypatch.setattr("modules.verification_pro.effective_for", AsyncMock(return_value={}))
    member = MagicMock(); member.guild = _guild()
    member.send = AsyncMock()
    assert asyncio.run(vv.send_verify_dm(member, {"mode": "button", "clone_id": None})) is True
    member.send.assert_awaited()
    member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "closed"))
    assert asyncio.run(vv.send_verify_dm(member, {"mode": "button", "clone_id": None})) is False


def _member_with_roles(unverified_id, has=True):
    m = MagicMock(); m.id = 77
    m.roles = [SimpleNamespace(id=unverified_id)] if has else []
    return m


def test_dm_button_verifies_and_retires_the_prompt(monkeypatch):
    import asyncio
    d = FakeDB(enabled=True, dm_verify=True, mode="button", unverified_role_id=9, channel_id=1)
    monkeypatch.setattr(vv, "db", d)
    guild = _guild(); member = _member_with_roles(9)
    guild.get_member.return_value = member
    client = MagicMock(); client.get_guild.return_value = guild; client.clone_id = None
    done = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(vv, "do_verify", done)
    retire = AsyncMock(); monkeypatch.setattr(vv, "_retire_dm_prompt", retire)
    i = _interaction(client)
    asyncio.run(vv.VerifyDmButton(5).callback(i))
    done.assert_awaited_once()
    assert "verified" in i.response.send_message.await_args.args[0].lower()
    retire.assert_awaited()


def test_dm_button_says_already_verified_and_does_not_reverify(monkeypatch):
    import asyncio
    d = FakeDB(enabled=True, dm_verify=True, mode="button", unverified_role_id=9, channel_id=1)
    monkeypatch.setattr(vv, "db", d)
    guild = _guild(); guild.get_member.return_value = _member_with_roles(9, has=False)
    client = MagicMock(); client.get_guild.return_value = guild; client.clone_id = None
    done = AsyncMock(); monkeypatch.setattr(vv, "do_verify", done)
    monkeypatch.setattr(vv, "_retire_dm_prompt", AsyncMock())
    i = _interaction(client)
    asyncio.run(vv.VerifyDmButton(5).callback(i))
    done.assert_not_awaited()
    assert "already verified" in i.response.send_message.await_args.args[0]


def test_dm_button_captcha_opens_the_question_modal_when_turnstile_is_off(monkeypatch):
    import asyncio
    d = FakeDB(enabled=True, dm_verify=True, mode="captcha", unverified_role_id=9, channel_id=1)
    monkeypatch.setattr(vv, "db", d)
    monkeypatch.setattr(vv, "_turnstile_ready", lambda: False)
    guild = _guild(); guild.get_member.return_value = _member_with_roles(9)
    client = MagicMock(); client.get_guild.return_value = guild; client.clone_id = None
    i = _interaction(client)
    asyncio.run(vv.VerifyDmButton(5).callback(i))
    modal = i.response.send_modal.await_args.args[0]
    assert isinstance(modal, vv.DmCaptchaModal) and re.match(r"^verify_dmcaptcha:5:\d:\d$", modal.custom_id)


def test_dm_captcha_wrong_answer_does_not_verify(monkeypatch):
    import asyncio
    modal = vv.DmCaptchaModal(5, 2, 3)
    modal.answer._value = "9"
    done = AsyncMock(); monkeypatch.setattr(vv, "do_verify", done)
    i = _interaction()
    asyncio.run(modal.on_submit(i))
    done.assert_not_awaited()


# ── join listener ────────────────────────────────────────────────────────

def _join(monkeypatch, cfg, dm_ok=True):
    import asyncio
    from discord_bot.cogs import verification as vcog
    monkeypatch.setattr(vcog, "db", SimpleNamespace(get_verification_config=AsyncMock(return_value=cfg)))
    monkeypatch.setattr(vcog.verification_pro, "effective_for", AsyncMock(return_value={}))
    sender = AsyncMock(return_value=dm_ok)
    monkeypatch.setattr(vcog, "send_verify_dm", sender)
    log = AsyncMock(); monkeypatch.setattr(vcog.verification_pro, "log_event", log)
    role = MagicMock()
    member = MagicMock(); member.bot = False; member.guild = _guild(); member.guild.get_role.return_value = role
    member.add_roles = AsyncMock()
    cog = vcog.VerificationCog.__new__(vcog.VerificationCog)
    cog.bot = SimpleNamespace(clone_id=None)
    asyncio.run(cog.on_member_join(member))
    return sender, log


def test_join_sends_the_dm_only_when_enabled_and_on(monkeypatch):
    base = {"enabled": True, "unverified_role_id": 9, "clone_id": None, "mode": "button"}
    sender, _ = _join(monkeypatch, {**base, "dm_verify": True})
    sender.assert_awaited_once()
    sender, _ = _join(monkeypatch, {**base, "dm_verify": False})
    sender.assert_not_awaited()


def test_closed_dms_fall_back_to_the_channel_and_are_logged(monkeypatch):
    sender, log = _join(monkeypatch, {"enabled": True, "unverified_role_id": 9, "clone_id": None, "mode": "button",
                                       "dm_verify": True}, dm_ok=False)
    sender.assert_awaited_once()
    log.assert_awaited_once()


def test_fits_discord_limits_with_the_find_by_name_helper_installed(fake):
    """On the live bot every channel dropdown gets a 'Find by name' row (modules/channel_finder.py); the setup must still fit."""
    import asyncio
    from modules import channel_finder
    channel_finder.install()
    fake.cfg.update(channel_id=1, unverified_role_id=2, verified_role_id=3, enabled=True, dm_verify=True, log_channel_id=4)
    for page in ("setup", "pro"):
        view = asyncio.run(vs.build_view(MagicMock(), 5, 3, page=page, note="x" * 200))
        assert len(list(view.walk_children())) <= 40
        view.to_components()
