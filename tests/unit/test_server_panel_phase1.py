"""Server Owners Panel Phase 1 tests: access, component limits, clone scoping,
premium display, failure paths. Style follows test_admin_panel_*.py."""
import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, MANAGER, MEMBER = 111, 222, 333
GUILD_ID = 5001
NOW = datetime.now(timezone.utc)
SP = "modules.server_panel"
VIEWS = "discord_bot.cogs._views_server_panel"


def make_db(premium_active=False):
    db = MagicMock()
    db.get_welcome_config = AsyncMock(return_value={
        "enabled": False, "channel_id": None, "message_template": "Hi {member}",
        "card_style": "gif", "card_theme": "wolf"})
    db.set_welcome_config = AsyncMock()
    db.get_welcome_extras = AsyncMock(return_value={
        "goodbye_enabled": False, "goodbye_channel_id": None,
        "goodbye_message": "{name} has left {guild}. We are now {count} members.",
        "member_role_id": None, "bot_role_id": None})
    db.set_welcome_extras = AsyncMock()
    db.get_verification_config = AsyncMock(return_value={
        "enabled": False, "mode": "button", "channel_id": None, "verified_role_id": None})
    db.set_verification_config = AsyncMock()
    db.get_honeypot_config = AsyncMock(return_value={"channel_id": None, "enabled": True})
    db.set_honeypot_config = AsyncMock()
    db.get_automod_config = AsyncMock(return_value={
        "action": "delete", "timeout_minutes": 10, "log_channel_id": None, "banned_words": [],
        "word_filter_enabled": False, "anti_invite_enabled": False,
        "anti_mention_enabled": False, "spam_enabled": False})
    db.set_automod_config = AsyncMock()
    db.get_leveling_config = AsyncMock(return_value={"announce_channel_id": None})
    db.get_ticket_config = AsyncMock(return_value={"panel_channel_id": None, "category_id": None})
    db.is_guild_premium_active = AsyncMock(return_value=premium_active)
    db.get_guild_premium = AsyncMock(
        return_value={"expires_at": NOW + timedelta(days=9)} if premium_active else None)
    db.get_setup_suggestions = AsyncMock(return_value={"dismissed": [], "custom_names": {}})
    return db


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    dbm.db = make_db()
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    for name in (SP, VIEWS):
        sys.modules.pop(name, None)
    sp = importlib.import_module(SP)
    views = importlib.import_module(VIEWS)
    # Keep the missing-channel scan out of these tests.
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    yield SimpleNamespace(sp=sp, views=views, db=dbm.db, dbm=dbm)
    for name in (SP, VIEWS):
        sys.modules.pop(name, None)


def guild(members=None):
    g = MagicMock()
    g.id = GUILD_ID
    g.name = "Test Server"
    g.member_count = 42
    g.owner_id = OWNER
    g.members = members or []
    g.get_member = MagicMock(return_value=None)
    return g


def interaction(user=OWNER, manage=True, clone_id=None, g=None):
    i = MagicMock()
    i.user.id = user
    i.guild = g or guild()
    i.guild_id = GUILD_ID
    i.permissions = SimpleNamespace(manage_guild=manage, administrator=False, manage_channels=manage)
    i.client.clone_id = clone_id
    i.client.is_ready = MagicMock(return_value=True)
    for n in ("send_message", "send_modal", "defer", "edit_message"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


def run(c):
    return asyncio.run(c)


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def all_views(v):
    return [v.HomeView, v.PremiumView, v.SetupView, v.WelcomeView, v.VerificationView, v.ModerationView]


# ── access ───────────────────────────────────────────────────────────────

def test_owner_and_manage_server_allowed_member_denied(env):
    g = guild()
    assert env.sp.access_denied_reason(g, OWNER, SimpleNamespace(manage_guild=False, administrator=False)) is None
    assert env.sp.access_denied_reason(g, MANAGER, SimpleNamespace(manage_guild=True, administrator=False)) is None
    reason = env.sp.access_denied_reason(g, MEMBER, SimpleNamespace(manage_guild=False, administrator=False))
    assert reason and "Manage Server" in reason
    assert env.sp.access_denied_reason(None, OWNER) is not None


def test_perm_guard_lockout_still_applies(env):
    from discord_bot.cogs import _perm_guard
    _perm_guard._cache.clear()
    members = []
    for _ in range(_perm_guard.MAX_PRIVILEGED_MEMBERS + 1):
        m = MagicMock()
        m.bot = False
        m.guild_permissions.administrator = True
        members.append(m)
    reason = env.sp.access_denied_reason(guild(members), OWNER, SimpleNamespace(manage_guild=True))
    assert reason and "Manage" in reason
    _perm_guard._cache.clear()


def test_access_revoked_mid_session(env):
    async def go():
        i = interaction(user=MANAGER)
        view = await env.views.HomeView.create(i)
        assert await view.interaction_check(i) is True
        revoked = interaction(user=MANAGER, manage=False)
        assert await view.interaction_check(revoked) is False
        revoked.response.send_message.assert_awaited()
    run(go())


def test_only_opener_can_click(env):
    async def go():
        i = interaction(user=OWNER)
        view = await env.views.HomeView.create(i)
        other = interaction(user=MANAGER)
        assert await view.interaction_check(other) is False
    run(go())


def test_modal_submit_rechecks_access(env):
    async def go():
        modal = env.views.WelcomeMessageModal("old", OWNER)
        modal.template._value = "new text"
        revoked = interaction(user=OWNER, manage=False)
        revoked.guild.owner_id = 999  # no longer the owner either
        await modal.on_submit(revoked)
        env.db.set_welcome_config.assert_not_awaited()
    run(go())


# ── component limits ─────────────────────────────────────────────────────

def test_component_and_row_limits(env):
    async def go():
        for cls in all_views(env.views):
            v = await cls.create(interaction())
            assert len(list(v.walk_children())) < env.views.MAX_COMPONENTS, cls.__name__
            for row in (c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5, cls.__name__
    run(go())


def test_every_screen_shows_reopen_hint(env):
    async def go():
        for cls in all_views(env.views):
            v = await cls.create(interaction())
            assert "/serversetup" in text_of(v), cls.__name__
    run(go())


# ── clone isolation ──────────────────────────────────────────────────────

def test_writes_carry_clone_id(env):
    async def go():
        i = interaction(clone_id=7)
        env.db.get_welcome_config.return_value["channel_id"] = 900
        view = await env.views.WelcomeView.create(i)
        await buttons(view)["Turn on"].callback(i)
        env.db.set_welcome_config.assert_awaited_once()
        assert env.db.set_welcome_config.await_args.kwargs["clone_id"] == 7
        assert env.db.set_welcome_config.await_args.kwargs["enabled"] is True
    run(go())


def test_reads_are_keyed_by_guild_and_clone(env):
    async def go():
        await env.views.ModerationView.create(interaction(clone_id=3))
        env.db.get_automod_config.assert_awaited_with(GUILD_ID, 3)
        env.db.get_honeypot_config.assert_awaited_with(GUILD_ID, 3)
    run(go())


# ── behaviour ────────────────────────────────────────────────────────────

def test_welcome_cannot_turn_on_without_channel(env):
    async def go():
        i = interaction()
        view = await env.views.WelcomeView.create(i)
        await buttons(view)["Turn on"].callback(i)
        env.db.set_welcome_config.assert_not_awaited()
        i.response.send_message.assert_awaited()
    run(go())


def test_moderation_toggle_writes_filter_flag(env):
    async def go():
        i = interaction()
        view = await env.views.ModerationView.create(i)
        await buttons(view)["Word filter: off"].callback(i)
        env.db.set_automod_config.assert_awaited_once_with(
            GUILD_ID, clone_id=None, word_filter_enabled=True)
    run(go())


def test_premium_lapsed_shows_locked_state(env):
    async def go():
        v = await env.views.PremiumView.create(interaction())
        assert "not active" in text_of(v)
        assert "Go Premium" in buttons(v)
        env.db.is_guild_premium_active.return_value = True
        env.db.get_guild_premium.return_value = {"expires_at": NOW + timedelta(days=5)}
        v2 = await env.views.PremiumView.create(interaction())
        assert "Renew" in buttons(v2)
    run(go())


def test_setup_checklist_scores_and_offers_fixes(env):
    async def go():
        env.db.get_automod_config.return_value["spam_enabled"] = True
        v = await env.views.SetupView.create(interaction())
        done, total = env.sp.setup_score(v.data["items"])
        assert total == 8 and 1 <= done < total
        assert "Welcome" in buttons(v)
        assert "Back" in buttons(v)
    run(go())


# ── failure paths ────────────────────────────────────────────────────────

def test_database_down_does_not_break_setup_screen(env):
    async def go():
        for name in ("get_welcome_config", "get_verification_config", "get_honeypot_config",
                     "get_automod_config", "get_leveling_config", "get_ticket_config",
                     "is_guild_premium_active", "get_guild_premium"):
            setattr(env.db, name, AsyncMock(side_effect=RuntimeError("db down")))
        sys.modules["discord_bot.cogs.setup_channels"].scan_missing_channels = AsyncMock(
            side_effect=RuntimeError("db down"))
        items = await env.sp.setup_items(guild(), None)
        assert len(items) == 8 and not any(i.done for i in items)
    run(go())


def test_audit_failure_never_raises(env):
    async def go():
        await env.sp.record_change(GUILD_ID, None, OWNER, "welcome.enabled", False, True)  # get_pool raises
    run(go())
