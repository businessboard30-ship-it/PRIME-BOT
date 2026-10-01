"""Honeypot: free core, premium extras, staff warned (not exempt, not actioned)."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

HP = "discord_bot.cogs.honeypot"
GUILD, CHAN, LOGCH = 5000, 6000, 7000
MEMBER, ADMIN, OWNER_ID, MOD = 1, 2, 3, 4


class FakeDB:
    def __init__(self):
        self.premium = False
        self.cfg = {"guild_id": GUILD, "clone_id": None, "channel_id": CHAN, "enabled": True,
                    "action": "ban", "delete_seconds": 86400, "log_channel_id": None,
                    "warning_message_id": None, "channel_auto_created": False,
                    "triggered_count": 0, "last_triggered_at": None, "created_by": None}
        self.writes = []
        self.premium_boom = False

    async def is_guild_premium_active(self, g, c):
        if self.premium_boom:
            raise RuntimeError("db down")
        return self.premium

    async def get_honeypot_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_honeypot_config(self, g, clone_id=None, **f):
        self.writes.append(f)
        self.cfg.update(f)
        return dict(self.cfg)

    async def bump_honeypot_triggers(self, g, clone_id=None):
        self.cfg["triggered_count"] += 1

    async def get_automod_config(self, g, clone_id=None):
        return {"log_channel_id": None}


@pytest.fixture()
def hp(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database")
    dbm.db = fake
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(HP, None)
    mod = importlib.import_module(HP)
    mod._cache.clear(); mod._warned.clear(); mod._tripping.clear()
    mod.fake = fake
    yield mod
    sys.modules.pop(HP, None)


def run(c):
    return asyncio.run(c)


def perms(**kw):
    p = discord.Permissions.none()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def make_msg(uid, p, *, bot=None):
    guild = MagicMock()
    guild.id, guild.owner_id = GUILD, OWNER_ID
    guild.name = "G"
    log = MagicMock(); log.send = AsyncMock()
    guild.get_channel = MagicMock(side_effect=lambda cid: log if cid == LOGCH else None)
    guild.ban = AsyncMock(); guild.kick = AsyncMock(); guild.unban = AsyncMock()
    member = MagicMock(spec=discord.Member)
    member.id, member.guild, member.guild_permissions = uid, guild, p
    member.mention, member.bot = f"<@{uid}>", False
    member.send = AsyncMock(); member.timeout = AsyncMock()
    member.created_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    member.joined_at = None
    member.top_role = 1                                   # below the bot's role
    guild.me.top_role = 10
    guild.me.guild_permissions = discord.Permissions.all()
    member.display_avatar.url = "http://x/y.png"
    msg = MagicMock()
    msg.guild, msg.author, msg.webhook_id = guild, member, None
    msg.content = "free nitro"; msg.attachments = []
    msg.channel.id = CHAN
    msg.channel.send = AsyncMock()
    msg.delete = AsyncMock()
    bot_ = bot or MagicMock(); bot_.clone_id = None
    return msg, guild, member, log, bot_


def cog_listener(hp, bot):
    c = hp.HoneypotCog(bot)
    return c


def feed(hp, msg, bot):
    async def go():
        c = hp.HoneypotCog(bot)
        await c.on_message(msg)
        await asyncio.sleep(0)   # let create_task(_release) start
    run(go())


REGULAR = perms(send_messages=True)
STAFF = perms(administrator=True)


# ── free core ────────────────────────────────────────────────────────────

def test_free_server_trap_still_enforces(hp):
    hp.fake.premium = False
    msg, guild, member, _, bot = make_msg(MEMBER, REGULAR)
    feed(hp, msg, bot)
    guild.ban.assert_awaited_once()
    assert guild.ban.await_args.kwargs["delete_message_seconds"] == 86400
    msg.delete.assert_awaited()
    assert hp.fake.cfg["triggered_count"] == 1


def test_free_server_ignores_saved_premium_extras(hp):
    hp.fake.premium = False
    hp.fake.cfg.update(action="timeout", delete_seconds=604800, log_channel_id=LOGCH)
    msg, guild, member, log, bot = make_msg(MEMBER, REGULAR)
    feed(hp, msg, bot)
    guild.ban.assert_awaited_once()                                  # free default, not the saved timeout
    assert guild.ban.await_args.kwargs["delete_message_seconds"] == 86400
    member.timeout.assert_not_awaited()
    log.send.assert_not_awaited()                                    # dedicated log channel is premium
    assert hp.fake.cfg["action"] == "timeout"                         # saved choice kept, just paused


def test_premium_server_uses_saved_extras(hp):
    hp.fake.premium = True
    hp.fake.cfg.update(action="timeout", log_channel_id=LOGCH)
    msg, guild, member, log, bot = make_msg(MEMBER, REGULAR)
    feed(hp, msg, bot)
    member.timeout.assert_awaited_once()
    guild.ban.assert_not_awaited()
    log.send.assert_awaited()


def test_premium_lookup_failure_falls_back_to_free_defaults_not_a_crash(hp):
    hp.fake.premium_boom = True
    hp.fake.cfg.update(action="timeout")
    msg, guild, member, _, bot = make_msg(MEMBER, REGULAR)
    feed(hp, msg, bot)
    guild.ban.assert_awaited_once()       # trap still protects the server


def test_paused_trap_does_nothing(hp):
    hp.fake.cfg["enabled"] = False
    msg, guild, member, _, bot = make_msg(MEMBER, REGULAR)
    feed(hp, msg, bot)
    guild.ban.assert_not_awaited(); msg.delete.assert_not_awaited()


def test_other_channels_untouched(hp):
    msg, guild, member, _, bot = make_msg(MEMBER, REGULAR)
    msg.channel.id = 1234
    feed(hp, msg, bot)
    guild.ban.assert_not_awaited(); msg.delete.assert_not_awaited()


# ── staff: not exempt, warned, never actioned ───────────────────────────

@pytest.mark.parametrize("uid,p", [(ADMIN, STAFF), (MOD, perms(ban_members=True)),
                                    (MOD, perms(manage_messages=True)), (OWNER_ID, perms())])
def test_staff_are_warned_not_actioned(hp, uid, p):
    hp.fake.premium = True
    msg, guild, member, log, bot = make_msg(uid, p)
    hp.fake.cfg["log_channel_id"] = LOGCH
    feed(hp, msg, bot)
    msg.delete.assert_awaited()                          # their message is removed
    msg.channel.send.assert_awaited_once()               # and they are told
    sent = msg.channel.send.await_args
    assert f"<@{uid}>" in sent.args[0] and "honeypot" in sent.args[0].lower()
    assert sent.kwargs["delete_after"] == hp.STAFF_WARNING_SECONDS
    guild.ban.assert_not_awaited(); guild.kick.assert_not_awaited(); member.timeout.assert_not_awaited()
    member.send.assert_not_awaited()                     # no "you were banned" DM
    assert hp.fake.cfg["triggered_count"] == 0           # staff aren't counted as catches
    assert "Warned" in str(log.send.await_args.kwargs["embed"].fields[0].value)


def test_staff_warning_only_pings_that_staff_member(hp):
    msg, guild, member, _, bot = make_msg(ADMIN, STAFF)
    feed(hp, msg, bot)
    am = msg.channel.send.await_args.kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and list(am.users) == [member]


def test_staff_warning_works_on_free_servers_too(hp):
    hp.fake.premium = False
    msg, guild, member, _, bot = make_msg(ADMIN, STAFF)
    feed(hp, msg, bot)
    msg.channel.send.assert_awaited_once()
    guild.ban.assert_not_awaited()


def test_staff_burst_gets_one_warning_but_every_message_removed(hp):
    bot = MagicMock(); bot.clone_id = None
    msg, guild, member, _, bot = make_msg(ADMIN, STAFF, bot=bot)
    async def go():
        c = hp.HoneypotCog(bot)
        for _ in range(3):
            await c.on_message(msg)
    run(go())
    assert msg.delete.await_count == 3
    assert msg.channel.send.await_count == 1


def test_staff_warning_survives_discord_errors(hp):
    msg, guild, member, _, bot = make_msg(ADMIN, STAFF)
    msg.delete.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    msg.channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    feed(hp, msg, bot)        # must not raise
    guild.ban.assert_not_awaited()


def test_bots_and_webhooks_still_ignored(hp):
    msg, guild, member, _, bot = make_msg(ADMIN, STAFF)
    msg.author.bot = True
    feed(hp, msg, bot)
    msg.delete.assert_not_awaited()
    msg2, *_rest = make_msg(MEMBER, REGULAR)
    msg2.webhook_id = 99
    feed(hp, msg2, bot)
    msg2.delete.assert_not_awaited()


# ── panel + authorization ────────────────────────────────────────────────

def _inner(c):
    return getattr(c, "item", c)


def labels(view):
    return {getattr(_inner(c), "label", None) for c in view.walk_children()
            if isinstance(_inner(c), discord.ui.Button)}


def kinds(view):
    return {type(c).__name__ for c in view.walk_children()}


def guild_for_panel():
    g = MagicMock(); g.id = GUILD
    g.get_channel = MagicMock(return_value=MagicMock())
    g.me.guild_permissions = discord.Permissions.all()
    return g


def test_free_panel_hides_extras_and_shows_upgrade(hp):
    view = hp.build_panel(guild_for_panel(), None, dict(hp.fake.cfg), premium=False)
    assert "Unlock premium extras" in labels(view)
    assert not any(isinstance(_inner(c), discord.ui.Select) for c in view.walk_children())
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "Free plan" in text and "Staff (mod/admin perms) get a warning" in text
    assert "Locked" not in text


def test_premium_panel_shows_selects_no_upgrade_button(hp):
    view = hp.build_panel(guild_for_panel(), None, dict(hp.fake.cfg), premium=True)
    assert "Unlock premium extras" not in labels(view)
    assert sum(isinstance(_inner(c), discord.ui.Select) for c in view.walk_children()) >= 2


def test_lapsed_server_is_told_its_saved_settings_are_paused(hp):
    cfg = dict(hp.fake.cfg, action="timeout")
    view = hp.build_panel(guild_for_panel(), None, cfg, premium=False)
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "paused, not lost" in text
    assert "Ban" in text and "Timeout" not in text.split("Action:")[1].split("\n")[0]


def test_panel_within_discord_limits(hp):
    for prem in (True, False):
        view = hp.build_panel(guild_for_panel(), None, dict(hp.fake.cfg), premium=prem)
        assert len(list(view.walk_children())) <= 40
        for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


def make_interaction(manage_guild=True, premium=False, client=None):
    hp.fake_premium = premium
    i = MagicMock()
    i.user.id = 77
    i.client = client or MagicMock()
    guild = guild_for_panel(); guild.owner = None
    member = MagicMock(); member.guild_permissions = perms(manage_guild=manage_guild)
    guild.get_member = MagicMock(return_value=member)
    i.client.get_guild = MagicMock(return_value=guild)
    i.client.clone_id = None
    i.response.is_done = MagicMock(return_value=False)
    i.response.send_message = AsyncMock(); i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i, guild


def test_authorize_is_free_but_needs_manage_server(hp):
    hp.fake.premium = False
    i, guild = make_interaction()
    assert run(hp._authorize(i, GUILD)) is guild                 # free server may open the honeypot
    i2, _ = make_interaction(manage_guild=False)
    assert run(hp._authorize(i2, GUILD)) is None
    i2.response.send_message.assert_awaited()


def test_authorize_with_premium_required_pitches_free_servers(hp, monkeypatch):
    hp.fake.premium = False
    pitch = AsyncMock()
    vm = types.ModuleType("discord_bot.cogs._views_premium"); vm.send_premium_pitch = pitch
    monkeypatch.setitem(sys.modules, "discord_bot.cogs._views_premium", vm)
    i, guild = make_interaction()
    assert run(hp._authorize(i, GUILD, need_premium=True)) is None
    pitch.assert_awaited_once()
    assert "premium" in i.followup.send.await_args.args[0].lower()
    hp.fake.premium = True
    i2, guild2 = make_interaction()
    assert run(hp._authorize(i2, GUILD, need_premium=True)) is guild2


@pytest.mark.parametrize("cls_name,kw", [("HoneypotActionSelect", {}), ("HoneypotHistorySelect", {}),
                                          ("HoneypotLogSelect", {})])
def test_extras_change_nothing_on_free_servers(hp, monkeypatch, cls_name, kw):
    hp.fake.premium = False
    pitch = AsyncMock()
    vm = types.ModuleType("discord_bot.cogs._views_premium"); vm.send_premium_pitch = pitch
    monkeypatch.setitem(sys.modules, "discord_bot.cogs._views_premium", vm)
    item = getattr(hp, cls_name)(GUILD, None)
    monkeypatch.setattr(type(item.item), "values", property(lambda self: ["kick"]), raising=False)
    i, guild = make_interaction()
    run(item.callback(i))
    assert hp.fake.writes == []
    pitch.assert_awaited_once()


def test_pause_resume_remove_and_repost_never_need_premium(hp, monkeypatch):
    hp.fake.premium = False
    pitch = AsyncMock()
    vm = types.ModuleType("discord_bot.cogs._views_premium"); vm.send_premium_pitch = pitch
    monkeypatch.setitem(sys.modules, "discord_bot.cogs._views_premium", vm)
    i, guild = make_interaction()
    i.edit_original_response = AsyncMock()
    run(hp.HoneypotButton("pause", GUILD, None).callback(i))
    assert hp.fake.cfg["enabled"] is False
    i2, _ = make_interaction(); i2.edit_original_response = AsyncMock()
    run(hp.HoneypotButton("pause", GUILD, None, paused=True).callback(i2))
    assert hp.fake.cfg["enabled"] is True
    i3, _ = make_interaction(); i3.edit_original_response = AsyncMock()
    run(hp.HoneypotButton("remove", GUILD, None).callback(i3))
    i3.edit_original_response.assert_awaited()
    pitch.assert_not_awaited()


def test_upgrade_button_pitches_free_servers_and_is_registered(hp, monkeypatch):
    hp.fake.premium = False
    pitch = AsyncMock()
    vm = types.ModuleType("discord_bot.cogs._views_premium"); vm.send_premium_pitch = pitch
    monkeypatch.setitem(sys.modules, "discord_bot.cogs._views_premium", vm)
    i, guild = make_interaction()
    run(hp.HoneypotButton("upgrade", GUILD, None).callback(i))
    pitch.assert_awaited_once()
    import re
    assert re.match(hp.HoneypotButton.__discord_ui_compiled_template__.pattern
                    if hasattr(hp.HoneypotButton, "__discord_ui_compiled_template__") else hp._pat("upgrade"),
                    hp._cid("upgrade", GUILD, None))


def test_command_description_no_longer_says_premium(hp):
    cmd = hp.HoneypotCog.honeypot_cmd
    assert "premium" not in cmd.description.lower()


def test_effective_config_is_pure(hp):
    saved = {"action": "kick", "delete_seconds": 3600, "log_channel_id": 9, "enabled": True}
    out = hp.effective_config(saved, False)
    assert out["action"] == "ban" and out["delete_seconds"] == 86400 and out["log_channel_id"] is None
    assert saved["action"] == "kick" and saved["log_channel_id"] == 9      # caller's copy untouched
    assert hp.effective_config(saved, True) is saved


# ── setup reply: Components v2 messages must not carry plain `content` ───

def test_open_honeypot_sends_view_only_no_content(hp, monkeypatch):
    chan = MagicMock(); chan.mention = "<#6000>"
    async def fake_ensure(guild, clone_id, user):
        return dict(hp.fake.cfg), chan, True, None
    monkeypatch.setattr(hp, "ensure_honeypot", fake_ensure)
    i, guild = make_interaction()
    run(hp.open_honeypot(i, guild, None))
    sent = i.followup.send.await_args
    assert sent.args == () and "content" not in sent.kwargs         # v2 views reject content
    assert sent.kwargs["ephemeral"] is True
    view = sent.kwargs["view"]
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "Created <#6000>" in text and "Honeypot" in text


def test_open_honeypot_existing_channel_note(hp, monkeypatch):
    chan = MagicMock(); chan.mention = "<#6000>"
    async def fake_ensure(guild, clone_id, user):
        return dict(hp.fake.cfg), chan, False, None
    monkeypatch.setattr(hp, "ensure_honeypot", fake_ensure)
    i, guild = make_interaction()
    run(hp.open_honeypot(i, guild, None))
    text = "\n".join(c.content for c in i.followup.send.await_args.kwargs["view"].walk_children()
                     if isinstance(c, discord.ui.TextDisplay))
    assert "Honeypot is live in <#6000>" in text


def test_open_honeypot_error_is_plain_text_without_a_view(hp, monkeypatch):
    async def fake_ensure(guild, clone_id, user):
        return None, None, False, "I need Manage Channels."
    monkeypatch.setattr(hp, "ensure_honeypot", fake_ensure)
    i, guild = make_interaction()
    run(hp.open_honeypot(i, guild, None))
    assert i.followup.send.await_args.args == ("I need Manage Channels.",)
    assert "view" not in i.followup.send.await_args.kwargs


@pytest.mark.parametrize("premium", [True, False])
def test_panel_with_note_serializes_as_components_v2(hp, premium):
    view = hp.build_panel(guild_for_panel(), None, dict(hp.fake.cfg), premium, note="🍯 note")
    comps = view.to_components()            # real discord.py serialization
    assert comps and view.has_components_v2()
    assert len(list(view.walk_children())) <= 40
