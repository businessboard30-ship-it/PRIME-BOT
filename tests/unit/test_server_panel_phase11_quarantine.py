"""Server Owners Panel Phase 11: quarantine role (one-tap release), channel lock, slow mode, join-gate quarantine action."""
import importlib
import sys
import types
from functools import total_ordering
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import GUILD_ID, MEMBER, OWNER, buttons, interaction, make_db, run

SP, SPQ, JG = "modules.server_panel", "modules.server_panel_quarantine", "modules.join_gate"
V1, V7 = "discord_bot.cogs._views_server_panel", "discord_bot.cogs._views_server_panel_p7"
NAMES = (SP, SPQ, V1, V7)
BOT_ID = 999
NO_PERMS = SimpleNamespace(administrator=False, manage_guild=False, ban_members=False, kick_members=False,
                           moderate_members=False, manage_messages=False)
STAFF_PERMS = SimpleNamespace(**{**NO_PERMS.__dict__, "kick_members": True})


@total_ordering
class FakeRole:
    def __init__(self, rid, position, managed=False, default=False):
        self.id, self.position, self.managed, self._default = rid, position, managed, default
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return self._default

    def __eq__(self, other):
        return isinstance(other, FakeRole) and self.id == other.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, other):
        return self.position < other.position


EVERYONE = FakeRole(1, 0, default=True)
QROLE = FakeRole(50, 5)
MOD_ROLE = FakeRole(60, 6)
ROLE_A, ROLE_B, BOOSTER = FakeRole(70, 2), FakeRole(71, 3), FakeRole(72, 2, managed=True)
BOT_ROLE = FakeRole(90, 20)


def member(uid, roles=(), perms=NO_PERMS, bot=False):
    rs = [EVERYONE, *roles]
    return SimpleNamespace(id=uid, bot=bot, roles=rs, mention=f"<@{uid}>", guild_permissions=perms,
                           top_role=max(rs), edit=AsyncMock(), add_roles=AsyncMock(), display_name=f"u{uid}")


def make_guild(members=(), roles=(QROLE, ROLE_A, ROLE_B, BOOSTER, MOD_ROLE), manage_roles=True):
    me = SimpleNamespace(id=BOT_ID, top_role=BOT_ROLE, guild_permissions=SimpleNamespace(manage_roles=manage_roles))
    by_role = {r.id: r for r in roles}
    by_member = {m.id: m for m in members}
    return SimpleNamespace(id=GUILD_ID, owner_id=OWNER, me=me, default_role=EVERYONE, channels=[],
                           get_role=lambda rid: by_role.get(rid), get_member=lambda uid: by_member.get(uid),
                           get_channel=MagicMock(return_value=None))


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_quarantine_role = AsyncMock(return_value=QROLE.id)
    db.set_quarantine_role = AsyncMock()
    db.add_quarantined = AsyncMock()
    db.get_quarantined = AsyncMock(return_value=None)
    db.list_quarantined = AsyncMock(return_value=[])
    db.remove_quarantined = AsyncMock()
    db.add_channel_lock = AsyncMock()
    db.get_channel_lock = AsyncMock(return_value=None)
    db.list_channel_locks = AsyncMock(return_value=[])
    db.remove_channel_lock = AsyncMock()
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no db"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS, cfg.PREMIUM_FEE_USD = 3, 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    for n in NAMES:
        sys.modules.pop(n, None)
    mods = dict(sp=importlib.import_module(SP), spq=importlib.import_module(SPQ),
                v1=importlib.import_module(V1), v7=importlib.import_module(V7))
    audit = AsyncMock()
    monkeypatch.setattr(mods["spq"], "record_change", audit)
    yield SimpleNamespace(db=db, audit=audit, **mods)
    for n in NAMES:
        sys.modules.pop(n, None)


# ── quarantine ───────────────────────────────────────────────────────────

def test_quarantine_swaps_roles_saves_the_rest_and_audits(env):
    target = member(5, [ROLE_A, ROLE_B, BOOSTER])
    g = make_guild([target])
    ok, msg = run(env.spq.quarantine_member(g, None, member(OWNER), target, "spam"))
    assert ok and "2 role(s) saved" in msg
    saved = env.db.add_quarantined.await_args.args[3]
    assert sorted(saved) == [ROLE_A.id, ROLE_B.id]           # booster (managed) role is not saved/removed
    new_roles = target.edit.await_args.kwargs["roles"]
    assert QROLE in new_roles and BOOSTER in new_roles and ROLE_A not in new_roles
    env.audit.assert_awaited()


@pytest.mark.parametrize("who,expect", [
    (member(5, bot=True), "Bots"),
    (member(OWNER), "owner"),
    (member(5, perms=STAFF_PERMS), "Staff"),
    (member(5, [QROLE]), "already"),
    (member(5, [BOT_ROLE]), "at or above mine"),
])
def test_quarantine_refuses_protected_targets(env, who, expect):
    g = make_guild([who], roles=(QROLE, BOT_ROLE))
    ok, msg = run(env.spq.quarantine_member(g, None, member(OWNER), who))
    assert not ok and expect in msg
    env.db.add_quarantined.assert_not_awaited()
    who.edit.assert_not_awaited()


def test_quarantine_actor_cannot_hit_equal_or_higher_rank(env):
    target = member(5, [MOD_ROLE])
    actor = member(7, [MOD_ROLE])
    ok, msg = run(env.spq.quarantine_member(make_guild([target]), None, actor, target))
    assert not ok and "below you" in msg


def test_quarantine_needs_a_working_role(env):
    env.db.get_quarantine_role.return_value = None
    ok, msg = run(env.spq.quarantine_member(make_guild(), None, member(OWNER), member(5)))
    assert not ok and "role first" in msg
    env.db.get_quarantine_role.return_value = QROLE.id
    ok, msg = run(env.spq.quarantine_member(make_guild(manage_roles=False), None, member(OWNER), member(5)))
    assert not ok and "Manage Roles" in msg


def test_quarantine_rolls_back_row_when_discord_refuses(env):
    target = member(5, [ROLE_A])
    target.edit.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    ok, _ = run(env.spq.quarantine_member(make_guild([target]), None, member(OWNER), target))
    assert not ok
    env.db.remove_quarantined.assert_awaited_once_with(GUILD_ID, None, 5)


def test_release_restores_only_roles_that_still_exist_and_are_assignable(env):
    target = member(5, [QROLE])
    env.db.get_quarantined.return_value = {"saved_role_ids": [ROLE_A.id, ROLE_B.id, 12345]}
    ok, msg = run(env.spq.release_member(make_guild([target]), None, OWNER, 5))
    assert ok and "2 role(s) restored" in msg and "1 no longer available" in msg
    roles = target.edit.await_args.kwargs["roles"]
    assert QROLE not in roles and ROLE_A in roles and ROLE_B in roles
    env.db.remove_quarantined.assert_awaited_once_with(GUILD_ID, None, 5)


def test_release_keeps_listing_when_discord_refuses(env):
    target = member(5, [QROLE])
    target.edit.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    env.db.get_quarantined.return_value = {"saved_role_ids": [ROLE_A.id]}
    ok, msg = run(env.spq.release_member(make_guild([target]), None, OWNER, 5))
    assert not ok and "stay on the list" in msg
    env.db.remove_quarantined.assert_not_awaited()


def test_release_of_someone_who_left_just_clears_the_row(env):
    env.db.get_quarantined.return_value = {"saved_role_ids": [ROLE_A.id]}
    ok, msg = run(env.spq.release_member(make_guild([]), None, OWNER, 5))
    assert ok and "left the server" in msg
    env.db.remove_quarantined.assert_awaited_once()


def test_release_unknown_person(env):
    ok, msg = run(env.spq.release_member(make_guild([]), None, OWNER, 5))
    assert not ok and "not on the quarantine list" in msg


def test_rejoin_while_quarantined_gets_the_role_back(env):
    m = member(5)
    m.guild = make_guild([m])
    env.db.get_quarantined.return_value = {"saved_role_ids": []}
    assert run(env.spq.apply_on_join(m, None)) is True
    m.add_roles.assert_awaited_once()
    other = member(6)
    other.guild = make_guild([other])
    env.db.get_quarantined.return_value = None
    assert run(env.spq.apply_on_join(other, None)) is False
    other.add_roles.assert_not_awaited()


def test_set_role_validates_before_writing(env):
    managed = FakeRole(80, 4, managed=True)
    err = run(env.spq.set_quarantine_role(make_guild(), None, OWNER, managed))
    assert err and "can't be handed out" in err
    env.db.set_quarantine_role.assert_not_awaited()
    above_bot = FakeRole(81, 30)
    assert "above the quarantine role" in run(env.spq.set_quarantine_role(make_guild(), None, OWNER, above_bot))
    assert run(env.spq.set_quarantine_role(make_guild(), None, OWNER, QROLE)) is None
    env.db.set_quarantine_role.assert_awaited_once_with(GUILD_ID, None, QROLE.id)


# ── channel lock / slow mode ─────────────────────────────────────────────

def text_channel(send=None, manage_roles=True, manage_channels=True):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.mention, ch.slowmode_delay = 33, "<#33>", 0
    ch.permissions_for = MagicMock(return_value=SimpleNamespace(manage_roles=manage_roles, manage_channels=manage_channels))
    ow = discord.PermissionOverwrite(send_messages=send)
    ch.overwrites_for = MagicMock(return_value=ow)
    ch.set_permissions, ch.edit = AsyncMock(), AsyncMock()
    return ch, ow


@pytest.mark.parametrize("before,saved", [(None, "none"), (True, "allow")])
def test_lock_saves_what_everyone_had(env, before, saved):
    ch, ow = text_channel(send=before)
    ok, _ = run(env.spq.lock_channel(make_guild(), None, OWNER, ch))
    assert ok and env.db.add_channel_lock.await_args.args[3] == saved
    assert ch.set_permissions.await_args.kwargs["overwrite"].send_messages is False


def test_lock_refusals(env):
    ch, _ = text_channel(send=False)
    ok, msg = run(env.spq.lock_channel(make_guild(), None, OWNER, ch))
    assert not ok and "already can't write" in msg
    ch, _ = text_channel()
    env.db.get_channel_lock.return_value = {"prev_send": "none"}
    ok, msg = run(env.spq.lock_channel(make_guild(), None, OWNER, ch))
    assert not ok and "already locked" in msg
    env.db.add_channel_lock.assert_not_awaited()
    ok, msg = run(env.spq.lock_channel(make_guild(), None, OWNER, MagicMock(spec=discord.VoiceChannel)))
    assert not ok and "text channels" in msg
    ch, _ = text_channel(manage_roles=False)
    env.db.get_channel_lock.return_value = None
    ok, msg = run(env.spq.lock_channel(make_guild(), None, OWNER, ch))
    assert not ok and "Manage Permissions" in msg


def test_lock_rolls_back_when_discord_refuses(env):
    ch, _ = text_channel()
    ch.set_permissions.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    ok, _ = run(env.spq.lock_channel(make_guild(), None, OWNER, ch))
    assert not ok
    env.db.remove_channel_lock.assert_awaited_once()


@pytest.mark.parametrize("saved,restored", [("none", None), ("allow", True), ("deny", False)])
def test_unlock_puts_back_the_original(env, saved, restored):
    ch, ow = text_channel(send=False)
    g = make_guild()
    g.get_channel = MagicMock(return_value=ch)
    env.db.get_channel_lock.return_value = {"prev_send": saved}
    ok, _ = run(env.spq.unlock_channel(g, None, OWNER, 33))
    assert ok and ch.set_permissions.await_args.kwargs["overwrite"].send_messages is restored
    env.db.remove_channel_lock.assert_awaited_once()


def test_unlock_leaves_hand_edited_overwrite_alone(env):
    ch, _ = text_channel(send=True)         # someone re-allowed it by hand
    g = make_guild()
    g.get_channel = MagicMock(return_value=ch)
    env.db.get_channel_lock.return_value = {"prev_send": "none"}
    ok, msg = run(env.spq.unlock_channel(g, None, OWNER, 33))
    assert ok and "by hand" in msg
    ch.set_permissions.assert_not_awaited()
    env.db.remove_channel_lock.assert_awaited_once()


def test_unlock_of_deleted_channel_and_unknown_lock(env):
    env.db.get_channel_lock.return_value = {"prev_send": "none"}
    ok, msg = run(env.spq.unlock_channel(make_guild(), None, OWNER, 33))
    assert ok and "no longer exists" in msg
    env.db.get_channel_lock.return_value = None
    ok, msg = run(env.spq.unlock_channel(make_guild(), None, OWNER, 33))
    assert not ok and "isn't locked" in msg


def test_slowmode_only_accepts_presets_and_needs_permission(env):
    ch, _ = text_channel()
    ok, _ = run(env.spq.set_slowmode(make_guild(), None, OWNER, ch, 7))
    assert not ok
    ch.edit.assert_not_awaited()
    ok, msg = run(env.spq.set_slowmode(make_guild(), None, OWNER, ch, 30))
    assert ok and "30 seconds" in msg and ch.edit.await_args.kwargs["slowmode_delay"] == 30
    ok, msg = run(env.spq.set_slowmode(make_guild(), None, OWNER, ch, 0))
    assert ok and "off" in msg
    ch2, _ = text_channel(manage_channels=False)
    ok, msg = run(env.spq.set_slowmode(make_guild(), None, OWNER, ch2, 10))
    assert not ok and "Manage Channel" in msg


# ── join gate + panel wiring ─────────────────────────────────────────────

def test_join_gate_accepts_quarantine_action():
    jg = importlib.import_module(JG)
    assert "quarantine" in jg.ACTIONS


def test_moderation_screen_links_to_quarantine_and_lockdown(env):
    v = run(env.v1.ModerationView.create(interaction()))
    assert any("Quarantine & lockdown" in label for label in buttons(v))


def test_quarantine_hub_screens_open(env):
    for name in ("ResponseView", "QuarantineView", "LockdownView"):
        v = run(getattr(env.v7, name).create(interaction()))
        assert "Back" in buttons(v)
