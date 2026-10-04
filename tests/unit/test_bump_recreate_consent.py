"""Deleted bump channel: ask once before creating a new one; no answer = nothing created, nothing more sent."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import bump

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def _http_error():
    return discord.HTTPException(MagicMock(status=403, reason="x"), "closed")


@pytest.fixture()
def env(monkeypatch):
    db = SimpleNamespace(
        bump_get_guild_config=AsyncMock(return_value=dict(configured_by=11, bump_channel_id=999)),
        bump_claim_recreate_ask=AsyncMock(return_value=True),
        bump_recreate_state=AsyncMock(return_value=dict(channel_recreate_declined=False, configured_by=11)),
        bump_decline_recreate=AsyncMock(),
    )
    monkeypatch.setattr(bump, "db", db)
    me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True))
    staff = MagicMock()
    staff.id, staff.bot, staff.send = 11, False, AsyncMock()
    owner = MagicMock()
    owner.id, owner.send = 1, AsyncMock()
    guild = MagicMock()
    guild.id, guild.name, guild.me, guild.owner, guild.owner_id = 77, "Cool Server", me, owner, 1
    guild.get_member = lambda uid: staff if uid == 11 else None
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "gone"))
    guild.create_text_channel = AsyncMock()
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.clone_id = None
    cog = bump.BumpCog.__new__(bump.BumpCog)
    cog.bot = bot
    cog._restore_one_guild = AsyncMock(return_value="needs_permission")
    return SimpleNamespace(db=db, guild=guild, cog=cog, staff=staff, owner=owner)


ROW = dict(target_guild_id=77, target_channel_id=999)


def test_deleted_channel_asks_by_dm_and_creates_nothing(env):
    assert run(env.cog._recover_target_channel(ROW, None)) is None
    env.guild.create_text_channel.assert_not_awaited()
    assert env.cog._restore_one_guild.await_args.kwargs == {"allow_create": False}
    env.staff.send.assert_awaited_once()                       # the person who set bumps up gets the question
    env.owner.send.assert_not_awaited()
    view = env.staff.send.await_args.kwargs["view"]
    ids = [c.item.custom_id for c in view.children]
    assert ids == ["bump:recreate:yes:77:-", "bump:recreate:no:77:-"]
    assert "won't create anything unless you tap Yes" in env.staff.send.await_args.args[0]


def test_it_asks_only_once_then_stays_quiet(env):
    run(env.cog._recover_target_channel(ROW, None))
    env.db.bump_claim_recreate_ask.return_value = False        # every later queued bump for this server
    for _ in range(5):
        assert run(env.cog._recover_target_channel(ROW, None)) is None
    assert env.staff.send.await_count == 1
    env.guild.create_text_channel.assert_not_awaited()


def test_closed_dms_fall_back_to_the_owner_then_to_silence(env):
    env.staff.send.side_effect = _http_error()
    run(env.cog._recover_target_channel(ROW, None))
    env.owner.send.assert_awaited_once()
    env.db.bump_claim_recreate_ask.return_value = True
    env.owner.send.reset_mock()
    env.owner.send.side_effect = _http_error()
    assert run(env.cog._recover_target_channel(ROW, None)) is None     # nobody reachable: quiet, no crash
    env.guild.create_text_channel.assert_not_awaited()


def test_no_question_when_the_bot_could_not_create_a_channel_anyway(env):
    env.guild.me.guild_permissions.manage_channels = False
    run(env.cog._recover_target_channel(ROW, None))
    env.staff.send.assert_not_awaited()
    env.db.bump_claim_recreate_ask.assert_not_awaited()


def test_an_existing_bump_channel_is_still_relinked_without_asking(env):
    ch = MagicMock(spec=discord.TextChannel)
    env.cog._restore_one_guild.return_value = "reused"
    env.db.bump_get_guild_config.side_effect = [dict(configured_by=11), dict(bump_channel_id=555)]
    env.cog.bot.get_channel.return_value = ch
    assert run(env.cog._recover_target_channel(ROW, None)) is ch
    env.staff.send.assert_not_awaited()


# ── the Yes / No buttons ─────────────────────────────────────────────────

def _press(env, action, user_id=11, perms=None, clone_id=None):
    btn = bump.DynamicBumpRecreateButton(action, 77, clone_id)
    member = MagicMock()
    member.id = user_id
    member.guild_permissions = perms or SimpleNamespace(administrator=False, manage_guild=True, manage_channels=False)
    env.guild.get_member = lambda uid: member if uid == user_id else None
    env.guild.get_channel = lambda cid: MagicMock(mention="#bump", send=AsyncMock())
    i = MagicMock()
    i.user.id = user_id
    i.client = env.cog.bot
    i.client.get_cog.return_value = env.cog
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    run(btn.callback(i))
    return i


def test_no_records_the_decline_and_creates_nothing(env):
    i = _press(env, "no")
    env.db.bump_decline_recreate.assert_awaited_once_with(77, None)
    env.cog._restore_one_guild.assert_not_awaited()
    assert "won't create" in i.edit_original_response.await_args.kwargs["content"]


def test_yes_creates_it_via_the_locked_never_duplicate_path(env):
    env.cog._restore_one_guild.return_value = "created"
    env.db.bump_get_guild_config.return_value = dict(bump_channel_id=555)
    i = _press(env, "yes")
    env.cog._restore_one_guild.assert_awaited_once()
    assert "allow_create" not in env.cog._restore_one_guild.await_args.kwargs      # default True: creation approved
    assert "Done" in i.edit_original_response.await_args.kwargs["content"]


def test_strangers_cannot_answer_for_the_server(env):
    i = _press(env, "yes", user_id=99999, perms=SimpleNamespace(administrator=False, manage_guild=False, manage_channels=False))
    env.cog._restore_one_guild.assert_not_awaited()
    assert "Only the server owner" in i.followup.send.await_args.args[0]


def test_answering_after_a_decline_changes_nothing(env):
    env.db.bump_recreate_state.return_value = dict(channel_recreate_declined=True, configured_by=11)
    _press(env, "yes")
    env.cog._restore_one_guild.assert_not_awaited()


def test_button_ids_roundtrip_including_clone_ids():
    for clone in (None, 4):
        cid = bump.DynamicBumpRecreateButton("yes", 77, clone).item.custom_id
        m = re.fullmatch(bump.DynamicBumpRecreateButton.__discord_ui_compiled_template__.pattern, cid)
        assert m and m["action"] == "yes" and m["guild_id"] == "77"
        assert (m["clone_id"] == "-") == (clone is None)


# ── restore helper + schema ──────────────────────────────────────────────

class _Ctx:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def test_restore_with_allow_create_false_never_creates_a_channel(monkeypatch):
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.transaction = lambda: _Ctx()
    pool = MagicMock()
    pool.acquire = lambda: _Ctx(conn)
    import database
    monkeypatch.setattr(database, "get_pool", AsyncMock(return_value=pool))
    import discord_bot.cogs.bump_setup as bs
    monkeypatch.setattr(bs, "find_existing_bump_channels", lambda g, chans: [])
    guild = MagicMock()
    guild.id = 5
    guild.fetch_channels = AsyncMock(return_value=[])
    guild.create_text_channel = AsyncMock()
    guild.me.guild_permissions.manage_channels = True
    cog = bump.BumpCog.__new__(bump.BumpCog)
    cog.bot = MagicMock()
    assert run(cog._restore_one_guild(guild, None, 1, allow_create=False)) == "needs_permission"
    guild.create_text_channel.assert_not_awaited()
    # ...and with creation approved the same call does create it
    guild.create_text_channel.return_value = MagicMock(id=42)
    monkeypatch.setattr(bump.db, "bump_set_guild_config", AsyncMock())
    run(cog._restore_one_guild(guild, None, 1))
    guild.create_text_channel.assert_awaited_once()


def test_migration_and_schema_and_reset_on_new_channel():
    sql = (ROOT / "database/migrations/030_bump_channel_recreate_consent.sql").read_text()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "DROP " not in code and code.count("ADD COLUMN") == code.count("ADD COLUMN IF NOT EXISTS") == 2
    src = (ROOT / "database.py").read_text()
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 52
    assert "030_bump_channel_recreate_consent.sql" in src
    upd = src[src.index("UPDATE bump_guild_config SET\n                        bump_channel_id"):][:700]
    assert "channel_recreate_asked_at = CASE WHEN $3::bigint IS NOT NULL THEN NULL" in upd
    assert "channel_recreate_declined = CASE WHEN $3::bigint IS NOT NULL THEN FALSE" in upd
    body = src[src.index("async def bump_decline_recreate"):][:700]
    assert "bump_channel_id = NULL" not in body          # keeping the id stops the boot auto-restore undoing a "no"
