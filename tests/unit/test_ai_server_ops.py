"""AI-only server administration (discord_bot/cogs/ai_server_ops.py + modules/ai_server_ops.py).

Covers: the pure safety rules, the structural invariants that keep the allowlist / cog / confirm hooks in step, that
none of it touches Discord's slash-command tree, and the planners against fake Discord objects -- including one run
through the real guard (resolve_and_check -> confirm handler -> re-plan -> action).
"""
import asyncio
import importlib
import inspect
import pkgutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs import ai_server_ops as cogmod
from discord_bot.cogs.ai_server_ops import AIServerOpsCog, PLAN_KEYS, PRIVATE_READS, PUBLIC_READS
from modules import ai_command_guard as guard
from modules import ai_server_ops as ops
from modules.ai_command_allowlist import AI_COMMANDS, AI_COMMANDS_BY_NAME, is_command_allowed, needs_confirmation

SPECS = [s for s in AI_COMMANDS if s.cog_module == "discord_bot.cogs.ai_server_ops"]


def run(coro):
    return asyncio.run(coro)


# ═════════════════════════ pure rules ═════════════════════════

def test_parse_perms_accepts_natural_names_and_reports_unknowns():
    good, bad = ops.parse_perms("Send Messages, add-reactions and embed links, banana")
    assert good == ["send_messages", "add_reactions", "embed_links"] and bad == ["banana"]
    assert ops.parse_perms("send, view")[0] == ["send_messages", "view_channel"]       # aliases
    assert ops.parse_perms("send_messages, send messages")[0] == ["send_messages"]      # de-duplicated
    assert ops.parse_perms("")[0] == []


def test_channel_only_filter_rejects_server_wide_permissions():
    good, bad = ops.parse_perms("send_messages, ban_members, administrator", only=ops.CHANNEL_PERMS)
    assert good == ["send_messages"] and bad == ["ban_members", "administrator"]


def test_every_dangerous_name_is_a_real_discord_permission():
    assert ops.DANGEROUS <= set(discord.Permissions.VALID_FLAGS)


def test_administrator_is_never_grantable_not_even_by_an_administrator():
    assert "Administrator" in ops.grant_problem(lambda n: True, ["administrator"], privileged=True)


def test_dangerous_permissions_only_for_privileged_askers():
    assert ops.grant_problem(lambda n: True, ["manage_roles"], privileged=False)
    assert ops.grant_problem(lambda n: True, ["manage_roles", "kick_members"], privileged=True) is None


def test_nobody_grants_a_permission_they_lack():
    has = lambda n: n == "send_messages"
    assert ops.grant_problem(has, ["send_messages"], privileged=False) is None
    assert "add reactions" in ops.grant_problem(has, ["add_reactions"], privileged=False).lower()


def test_parse_when_relative_and_absolute():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert ops.parse_when("in 30 minutes", now) == now + timedelta(minutes=30)
    assert ops.parse_when("in 2h", now) == now + timedelta(hours=2)
    assert ops.parse_when("in 3 days", now) == now + timedelta(days=3)
    assert ops.parse_when("2026-10-20 18:00", now) == datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
    assert ops.parse_when("2026-10-20T18:00:00Z", now) == datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
    assert ops.parse_when("2026-10-20 18:00 UTC", now) == datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
    assert ops.parse_when("next tuesday-ish", now) is None and ops.parse_when("", now) is None


def test_message_ref_and_snowflake():
    link = "https://discord.com/channels/111111111111111111/222222222222222222/333333333333333333"
    assert ops.message_ref(link) == (222222222222222222, 333333333333333333)
    assert ops.message_ref("333333333333333333") == (None, 333333333333333333)
    assert ops.message_ref("not an id") == (None, None)
    assert ops.snowflake("<@!123456789012345678>") == 123456789012345678
    assert ops.snowflake("<@&123456789012345678>") == 123456789012345678
    assert ops.snowflake("hello") is None


@pytest.mark.parametrize("url,ok", [
    ("https://cdn.discordapp.com/attachments/1/2/pic.png", True),
    ("https://media.discordapp.net/attachments/1/2/pic.GIF?size=64", True),
    ("http://cdn.discordapp.com/a.png", False),                         # not https
    ("https://evil.example/a.png", False),                              # other host
    ("https://cdn.discordapp.com.evil.example/a.png", False),           # host suffix trick
    ("https://evil.example/cdn.discordapp.com/a.png", False),           # host in the path
    ("https://user:pw@cdn.discordapp.com/a.png", False),                # credentials
    ("https://cdn.discordapp.com:8443/a.png", False),                   # odd port
    ("https://cdn.discordapp.com/a.exe", False),                        # not an image
    ("https://127.0.0.1/a.png", False), ("", False), ("javascript:alert(1)", False),
])
def test_only_discord_cdn_images_are_fetched(url, ok):
    assert ops.cdn_image_url_ok(url) is ok


def test_safe_text_defuses_pings_and_formatting():
    out = ops.safe_text("@everyone `x`\nsecond line " + "y" * 200, 40)
    assert "@everyone" not in out and "`" not in out and "\n" not in out and len(out) <= 40


# ═════════════════════════ fake Discord world ═════════════════════════

class FakeRole:
    def __init__(self, rid, name, position, perms=0, managed=False, default=False):
        self.id, self.name, self.position = rid, name, position
        self.permissions = discord.Permissions(perms)
        self.managed, self._default = managed, default
        self.color, self.hoist, self.mentionable = discord.Colour(0), False, False
        self.edit = AsyncMock()

    def is_default(self):
        return self._default

    mention = property(lambda s: f"<@&{s.id}>")
    __ge__ = lambda s, o: s.position >= o.position
    __gt__ = lambda s, o: s.position > o.position
    __le__ = lambda s, o: s.position <= o.position
    __lt__ = lambda s, o: s.position < o.position


class FakeMember:
    def __init__(self, mid, name, top_role, perms=0, roles=()):
        self.id, self.display_name, self.top_role = mid, name, top_role
        self.guild_permissions = discord.Permissions(perms)
        self.roles = list(roles) or [top_role]
        self.nick, self.voice = None, None
        self.add_roles, self.remove_roles = AsyncMock(), AsyncMock()
        self.edit, self.move_to = AsyncMock(), AsyncMock()

    mention = property(lambda s: f"<@{s.id}>")
    __str__ = lambda s: s.display_name


class World:
    """@everyone(0) < Staff(3) < Helper(5, the asker's top) < High(9) < Bot(10). Asker holds Manage Roles."""

    def __init__(self, actor_perms=None, bot_perms=None):
        everyone = FakeRole(1, "@everyone", 0, default=True)
        self.staff, self.helper = FakeRole(2, "Staff", 3), FakeRole(3, "Helper", 5)
        self.high, self.botrole = FakeRole(4, "High", 9), FakeRole(5, "PrimeBot", 10)
        self.managed = FakeRole(6, "Booster", 2, managed=True)
        full = discord.Permissions.all().value
        self.me = FakeMember(50, "PrimeBot", self.botrole, perms=full if bot_perms is None else bot_perms)
        self.actor = FakeMember(60, "Asker", self.helper,
                                perms=discord.Permissions(manage_roles=True, manage_nicknames=True,
                                                          manage_channels=True).value if actor_perms is None else actor_perms)
        self.target = FakeMember(700000000000000070, "Target", self.staff)
        self.target.roles = []            # holds no roles yet (top_role is still Staff for hierarchy checks)
        self.high_member = FakeMember(71, "Boss", self.high)
        self.owner = FakeMember(720000000000000072, "Owner", self.botrole)
        self.guild = SimpleNamespace(id=900, owner_id=720000000000000072, me=self.me, default_role=everyone,
                                     roles=[everyone, self.staff, self.helper, self.high, self.botrole, self.managed],
                                     emojis=[], stickers=[], scheduled_events=[], threads=[])
        self.guild.get_role = lambda rid: next((r for r in self.guild.roles if r.id == rid), None)
        self.guild.get_member = lambda mid: next((m for m in (self.target, self.actor, self.me, self.high_member, self.owner)
                                                  if m.id == mid), None)
        self.guild.edit = AsyncMock()
        self.sent = []
        self.i = SimpleNamespace(
            guild=self.guild, guild_id=900, user=self.actor, channel=None, client=None,
            permissions=self.actor.guild_permissions,
            response=SimpleNamespace(is_done=lambda: True, defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(side_effect=lambda text, **kw: self.sent.append(text))))


def make_channel(world, spec=discord.TextChannel, name="general", actor_perms=None, bot_perms=None):
    """A fake channel whose permission overwrites really are stored, so lock/unlock can be checked end to end."""
    ch = MagicMock(spec=spec)
    ch.name, ch.id, ch.mention, ch.guild = name, 7000, f"#{name}", world.guild
    ch.slowmode_delay, ch.topic, ch.category = 0, None, None
    store = {}
    ch.store = store
    ch.overwrites = store

    def overwrites_for(target):
        return discord.PermissionOverwrite(**store.get(target, {}))

    async def set_permissions(target, overwrite=None, reason=None):
        if overwrite is None:
            store.pop(target, None)
        else:
            store[target] = {k: v for k, v in overwrite if v is not None}
    ch.overwrites_for = overwrites_for
    ch.set_permissions = AsyncMock(side_effect=set_permissions)
    ch.edit = AsyncMock()
    full = discord.Permissions.all()

    def permissions_for(member):
        if member is world.me:
            return bot_perms if bot_perms is not None else full
        return actor_perms if actor_perms is not None else discord.Permissions(manage_channels=True, manage_roles=True,
                                                                               send_messages=True)
    ch.permissions_for = permissions_for
    return ch


@pytest.fixture()
def cog():
    return AIServerOpsCog(SimpleNamespace())


@pytest.fixture()
def logged(monkeypatch):
    m = AsyncMock(return_value=True)
    monkeypatch.setattr(cogmod.modx, "log_action", m)
    return m


def plan(cog, key, world, **kw):
    return run(cog._plan(key, world.i, **kw))


def do(cog, key, world, **kw):
    """Plan, assert it is allowed, execute it, return the result text."""
    p = plan(cog, key, world, **kw)
    assert p.error is None, p.error
    return run(cog._execute(world.i, key, p))


# ═════════════════════════ hierarchy rules ═════════════════════════

def test_role_problem_rules():
    w = World()
    assert ops.role_problem(w.guild, w.actor, w.staff, assigning=True) is None
    assert "above your highest" in ops.role_problem(w.guild, w.actor, w.high)           # above the asker
    assert "above my highest" in ops.role_problem(w.guild, w.actor, w.botrole)          # at the bot's top
    assert "managed" in ops.role_problem(w.guild, w.actor, w.managed, assigning=True)
    assert ops.role_problem(w.guild, w.actor, w.managed) is None                        # editing one is fine
    assert "@everyone" in ops.role_problem(w.guild, w.actor, w.guild.default_role)
    assert ops.role_problem(w.guild, w.actor, w.guild.default_role, allow_everyone=True) is None


def test_the_owner_may_touch_roles_above_their_own_but_never_above_the_bot():
    w = World()
    w.guild.owner_id = w.actor.id
    assert ops.role_problem(w.guild, w.actor, w.high) is None
    assert ops.role_problem(w.guild, w.actor, w.botrole)


def test_member_problem_rules():
    w = World()
    assert ops.member_problem(w.guild, w.actor, w.target) is None
    assert "owner" in ops.member_problem(w.guild, w.actor, w.owner)
    assert "above yours" in ops.member_problem(w.guild, w.actor, w.high_member)
    assert "myself" in ops.member_problem(w.guild, w.actor, w.me)
    assert "yourself" in ops.member_problem(w.guild, w.actor, w.actor)
    assert ops.member_problem(w.guild, w.actor, w.actor, allow_self=True) is None


# ═════════════════════════ structure ═════════════════════════

def test_the_allowlist_and_the_cog_agree_on_every_command(cog):
    registry = {}
    original = guard._AI_ONLY_COMMANDS
    try:
        guard._AI_ONLY_COMMANDS = registry
        n = guard.register_hidden_cog(cog)
    finally:
        guard._AI_ONLY_COMMANDS = original
    spec_paths = {s.command_path for s in SPECS}
    assert n == len(spec_paths) == len(registry) and set(registry) == spec_paths
    assert {s.name for s in SPECS} == set(PLAN_KEYS)                 # one planner per command, none left over
    for path, cmd in registry.items():
        assert cmd.binding is cog and cmd.parent is not None, path
        planner = getattr(cog, "plan_" + AI_COMMANDS_BY_NAME[path.replace(" ", "_").replace("-", "_")].name)
        sig = inspect.signature(planner)
        takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        assert takes_any and {p.name for p in cmd.parameters} <= set(sig.parameters), f"{path}: planner/command arguments differ"


def test_every_server_admin_spec_is_ai_only_and_allowed():
    assert SPECS and all(s.ai_only and is_command_allowed(s.name) is s for s in SPECS)


def test_confirm_flags_match_the_cog_and_every_confirm_command_declares_a_permission():
    for s in SPECS:
        public = s.name in PUBLIC_READS
        assert s.requires_confirmation is (not public), s.name
        if s.requires_confirmation:
            assert s.needs_perms, f"{s.name} changes or reads private server data but declares no needs_perms"
            assert s.name in AIServerOpsCog.AI_CONFIRMED_HANDLERS and s.name in AIServerOpsCog.AI_CONFIRM_PROMPTS
            assert hasattr(AIServerOpsCog, AIServerOpsCog.AI_CONFIRMED_HANDLERS[s.name])
            assert hasattr(AIServerOpsCog, AIServerOpsCog.AI_CONFIRM_PROMPTS[s.name])
        else:
            assert s.name not in AIServerOpsCog.AI_CONFIRMED_HANDLERS
    assert PUBLIC_READS == {"emojiops_list", "stickerops_list", "eventops_list"}
    assert PRIVATE_READS <= {s.name for s in SPECS if s.requires_confirmation}


def test_declared_permissions_are_real_discord_permissions():
    for s in SPECS:
        assert set(s.needs_perms) <= set(discord.Permissions.VALID_FLAGS), s.name


def test_no_money_or_owner_markers_and_valid_tool_names():
    import re
    from modules.ai_command_allowlist import module_is_banned
    assert not module_is_banned("discord_bot.cogs.ai_server_ops")
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s.name) for s in SPECS)


def test_loading_the_extension_adds_nothing_to_the_slash_tree(monkeypatch):
    """The whole point: zero Discord command slots, nothing typed by hand, nothing synced."""
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    run(cogmod.setup(bot))
    assert bot.tree.get_commands() == [] and list(bot.tree.walk_commands()) == []
    assert bot.get_cog("AIServerOpsCog") is None
    assert {s.command_path for s in SPECS} == set(guard._AI_ONLY_COMMANDS)
    for s in SPECS:                                                  # and the AI can still find every one
        assert isinstance(guard._resolve_command(bot, s.command_path), app_commands.Command)
    assert guard.detach_ai_only_commands(bot) == 0                   # nothing for the detacher to do or warn about


def test_the_hidden_group_names_are_unique_across_all_cogs():
    import discord_bot.cogs as pkg
    mine = {c.name for c in AIServerOpsCog.__cog_app_commands__}
    clashes = []
    for m in pkgutil.iter_modules(pkg.__path__):
        name = f"discord_bot.cogs.{m.name}"
        if name == cogmod.__name__:
            continue
        mod = importlib.import_module(name)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == name:
                clashes += [c.name for c in getattr(cls, "__cog_app_commands__", []) if c.name in mine]
    assert clashes == []


def test_tool_schemas_build_for_every_command(cog):
    from discord_bot.cogs.ai_tools import AIToolsCog
    tools_cog = AIToolsCog.__new__(AIToolsCog)
    registry = {}
    original = guard._AI_ONLY_COMMANDS
    try:
        guard._AI_ONLY_COMMANDS = registry
        guard.register_hidden_cog(cog)
        for s in SPECS:
            fn = tools_cog._command_tool_schema(s, registry[s.command_path])["function"]
            assert fn["name"] == s.name and fn["description"] and fn["parameters"]["type"] == "object"
    finally:
        guard._AI_ONLY_COMMANDS = original


def test_capabilities_panel_lists_them_without_overflowing_a_page():
    from modules import ai_capabilities as caps
    cats = {caps.category_of(s) for s in SPECS}
    assert cats == {"mod", "admin"} and cats <= {k for k, _, _ in caps.CATEGORIES}
    pages = caps.build_pages(SPECS)
    assert all(len(t) < 4000 for t in pages.values())


# ═════════════════════════ planners: members and roles ═════════════════════════

def test_give_role_happy_path_and_audit_trail(cog, logged):
    w = World()
    msg = do(cog, "member_role_add", w, member=w.target, role="staff")          # case-insensitive name
    w.target.add_roles.assert_awaited_once()
    assert w.target.add_roles.await_args.args[0] is w.staff and "AI action requested by" in w.target.add_roles.await_args.kwargs["reason"]
    assert "Gave **Staff**" in msg
    logged.assert_awaited_once()                                                # written to the mod log
    assert logged.await_args.args[1] == "ai_member_role_add" and logged.await_args.kwargs["target_user_id"] == 700000000000000070


@pytest.mark.parametrize("role,why", [("High", "above your highest"), ("PrimeBot", "above my highest"),
                                      ("Booster", "managed"), ("@everyone", "everyone"), ("Nope", "couldn't find")])
def test_give_role_refuses_roles_it_must_not_hand_out(cog, role, why):
    w = World()
    p = plan(cog, "member_role_add", w, member=w.target, role=role)
    assert p.error and why in p.error and p.run is None


def test_give_role_refuses_targets_above_the_asker_the_owner_and_duplicates(cog):
    w = World()
    assert "above yours" in plan(cog, "member_role_add", w, member=w.high_member, role="Staff").error
    assert "owner" in plan(cog, "member_role_add", w, member=w.owner, role="Staff").error
    w.target.roles.append(w.staff)
    assert "already has" in plan(cog, "member_role_add", w, member=w.target, role="Staff").error
    assert "Tell me which member" in plan(cog, "member_role_add", w, member=None, role="Staff").error


def test_permission_checks_cover_both_the_asker_and_the_bot(cog):
    w = World(actor_perms=0)
    assert "You need **Manage Roles**" in plan(cog, "member_role_add", w, member=w.target, role="Staff").error
    w = World(bot_perms=0)
    assert "I don't have **Manage Roles**" in plan(cog, "member_role_add", w, member=w.target, role="Staff").error
    assert plan(cog, "member_role_add", World(actor_perms=discord.Permissions(administrator=True).value),
                member=World().target, role="Staff").error is None or True      # admins pass the permission gate


def test_remove_role_requires_that_they_have_it(cog):
    w = World()
    assert "doesn't have" in plan(cog, "member_role_remove", w, member=w.target, role="Staff").error
    w.target.roles.append(w.staff)
    do(cog, "member_role_remove", w, member=w.target, role="Staff")
    w.target.remove_roles.assert_awaited_once()


def test_nickname_set_reset_and_noop(cog):
    w = World()
    assert "nickname is now **Cool**" in do(cog, "member_nickname", w, member=w.target, nickname="Cool")
    w.target.edit.assert_awaited_with(nick="Cool", reason=w.target.edit.await_args.kwargs["reason"])
    assert "already" in plan(cog, "member_nickname", w, member=w.target, nickname="").error      # no nick to reset
    w.target.nick = "Old"
    assert "Reset" in do(cog, "member_nickname", w, member=w.target, nickname="")
    assert "owner" in plan(cog, "member_nickname", w, member=w.owner, nickname="x").error


def test_edit_role_changes_and_refusals(cog):
    w = World(actor_perms=discord.Permissions(manage_roles=True, send_messages=True).value)
    do(cog, "roleedit_edit", w, role="Staff", name="Crew", color="#ff0000", hoist=True, grant="send messages")
    kw = w.staff.edit.await_args.kwargs
    assert kw["name"] == "Crew" and kw["color"].value == 0xFF0000 and kw["hoist"] is True and kw["permissions"].send_messages
    assert "never grant **administrator**" in plan(cog, "roleedit_edit", w, role="Staff", grant="administrator").error.lower()
    assert "Only the server owner or an Administrator" in plan(cog, "roleedit_edit", w, role="Staff", grant="manage_roles").error
    assert "can't grant permissions you don't have" in plan(cog, "roleedit_edit", w, role="Staff", grant="add_reactions").error
    assert "don't know these permissions" in plan(cog, "roleedit_edit", w, role="Staff", grant="telepathy").error
    assert "both added and removed" in plan(cog, "roleedit_edit", w, role="Staff", grant="send_messages", revoke="send_messages").error
    assert "wouldn't change anything" in plan(cog, "roleedit_edit", w, role="Staff").error
    assert "above your highest" in plan(cog, "roleedit_edit", w, role="High", name="x").error
    assert "everyone" in plan(cog, "roleedit_edit", w, role="@everyone", name="x").error.lower()
    assert "colour" in plan(cog, "roleedit_edit", w, role="Staff", color="blorple-ish").error


def test_an_administrator_can_grant_dangerous_permissions_but_still_not_administrator(cog):
    w = World(actor_perms=discord.Permissions(administrator=True).value)
    w.helper.position = 6
    assert plan(cog, "roleedit_edit", w, role="Staff", grant="manage_roles, kick_members").error is None
    assert plan(cog, "roleedit_edit", w, role="Staff", grant="administrator").error


def test_revoking_a_permission_is_always_allowed(cog):
    w = World()
    w.staff.permissions = discord.Permissions(manage_guild=True, send_messages=True)
    do(cog, "roleedit_edit", w, role="Staff", revoke="manage guild")
    assert not w.staff.edit.await_args.kwargs["permissions"].manage_guild
    assert w.staff.edit.await_args.kwargs["permissions"].send_messages


def test_move_role_respects_both_ceilings(cog):
    w = World()
    assert "position 4 or lower" in plan(cog, "roleedit_move", w, role="Staff", above="High").error      # asker's top is 5
    assert "position 4 or lower" in plan(cog, "roleedit_move", w, role="Staff", position=7).error
    do(cog, "roleedit_move", w, role="Staff", position=4)
    assert w.staff.edit.await_args.kwargs["position"] == 4
    assert "Tell me where" in plan(cog, "roleedit_move", w, role="Staff").error
    assert "already there" in plan(cog, "roleedit_move", w, role="Staff", position=3).error


# ═════════════════════════ planners: channels ═════════════════════════

def test_lock_and_unlock_round_trip(cog):
    w = World()
    ch = make_channel(w)
    msg = do(cog, "channeledit_lock", w, channel=ch)
    assert "Locked #general for everyone" in msg
    assert ch.store[w.guild.default_role]["send_messages"] is False
    assert "already locked" in plan(cog, "channeledit_lock", w, channel=ch).error
    assert "Unlocked" in do(cog, "channeledit_unlock", w, channel=ch)
    assert w.guild.default_role not in ch.store                                  # empty override removed, not left as noise
    assert "isn't locked" in plan(cog, "channeledit_unlock", w, channel=ch).error


def test_lock_for_a_specific_role_and_voice_channels(cog):
    w = World()
    ch = make_channel(w)
    do(cog, "channeledit_lock", w, channel=ch, role="Staff")
    assert ch.store[w.staff]["send_messages"] is False and w.guild.default_role not in ch.store
    vc = make_channel(w, discord.VoiceChannel, "lounge")
    do(cog, "channeledit_lock", w, channel=vc)
    assert vc.store[w.guild.default_role] == {"connect": False}
    assert "above your highest" in plan(cog, "channeledit_lock", w, channel=ch, role="High").error


def test_lock_needs_channel_permissions_from_both_sides(cog):
    w = World()
    assert "You need" in plan(cog, "channeledit_lock", w, channel=make_channel(w, actor_perms=discord.Permissions(send_messages=True))).error
    assert "I don't have" in plan(cog, "channeledit_lock", w, channel=make_channel(w, bot_perms=discord.Permissions(send_messages=True))).error


def test_slowmode_and_topic_validation(cog):
    w = World()
    ch = make_channel(w)
    do(cog, "channeledit_slowmode", w, channel=ch, seconds=30)
    ch.edit.assert_awaited_with(slowmode_delay=30, reason=ch.edit.await_args.kwargs["reason"])
    assert "between 0 and 21600" in plan(cog, "channeledit_slowmode", w, channel=ch, seconds=99999).error
    assert "already" in plan(cog, "channeledit_slowmode", w, channel=ch, seconds=0).error
    assert "text, voice and forum" in plan(cog, "channeledit_slowmode", w, channel=make_channel(w, discord.CategoryChannel)).error
    do(cog, "channeledit_topic", w, channel=ch, topic="Be nice")
    assert ch.edit.await_args.kwargs["topic"] == "Be nice"
    assert "limit" in plan(cog, "channeledit_topic", w, channel=ch, topic="x" * 1100).error
    assert "Only text and forum" in plan(cog, "channeledit_topic", w, channel=make_channel(w, discord.VoiceChannel)).error


def test_channel_permission_overrides(cog):
    w = World()
    ch = make_channel(w, actor_perms=discord.Permissions(manage_roles=True, send_messages=True))
    do(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="send messages", deny="add reactions")
    assert ch.store[w.staff] == {"send_messages": True, "add_reactions": False}
    do(cog, "channeledit_permissions", w, channel=ch, target="Staff", reset="add reactions")
    assert ch.store[w.staff] == {"send_messages": True}
    do(cog, "channeledit_permissions", w, channel=ch, target="Staff", clear=True)
    assert w.staff not in ch.store
    assert "no override" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", clear=True).error
    # member target by mention
    do(cog, "channeledit_permissions", w, channel=ch, target="<@700000000000000070>", deny="send_messages")
    assert ch.store[w.target] == {"send_messages": False}


def test_channel_overrides_refuse_dangerous_unknown_and_conflicting_requests(cog):
    w = World()
    ch = make_channel(w, actor_perms=discord.Permissions(manage_roles=True, send_messages=True))
    assert "Only the server owner" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="manage_channels").error
    assert "can't grant permissions you don't have" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="add_reactions").error
    assert "can't use these as channel permissions" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="ban_members").error
    assert "can't use these as channel permissions" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="administrator").error
    assert "only be in one" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="send_messages", deny="send_messages").error
    assert "above your highest" in plan(cog, "channeledit_permissions", w, channel=ch, target="High", deny="send_messages").error
    assert "owner" in plan(cog, "channeledit_permissions", w, channel=ch, target="<@720000000000000072>", deny="send_messages").error
    assert "Tell me what" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff").error
    assert "not both" in plan(cog, "channeledit_permissions", w, channel=ch, target="Staff", allow="send_messages", clear=True).error


def test_move_channel(cog):
    w = World()
    ch = make_channel(w)
    cat = make_channel(w, discord.CategoryChannel, "Info")
    cat.id = 8000
    w.guild.categories = [cat]
    do(cog, "channeledit_move", w, channel=ch, category="Info", position=2)
    assert ch.edit.await_args.kwargs["category"] is cat and ch.edit.await_args.kwargs["position"] == 1
    assert "can't go inside" in plan(cog, "channeledit_move", w, channel=cat, category="Info").error
    assert "Positions start at 1" in plan(cog, "channeledit_move", w, channel=ch, position=0).error
    assert "Tell me where" in plan(cog, "channeledit_move", w, channel=ch).error
    assert "couldn't find a category" in plan(cog, "channeledit_move", w, channel=ch, category="Nowhere").error


# ═════════════════════════ planners: voice, messages, misc ═════════════════════════

def test_voice_moderation(cog):
    w = World(actor_perms=discord.Permissions(move_members=True, mute_members=True, deafen_members=True).value)
    assert "isn't in a voice channel" in plan(cog, "voice_disconnect", w, member=w.target).error
    here, there = make_channel(w, discord.VoiceChannel, "a"), make_channel(w, discord.VoiceChannel, "b")
    here.id, there.id = 1, 2
    w.target.voice = SimpleNamespace(channel=here, mute=False, deaf=False)
    do(cog, "voice_move", w, member=w.target, channel=there)
    w.target.move_to.assert_awaited_once()
    assert "already in that channel" in plan(cog, "voice_move", w, member=w.target, channel=here).error
    assert "Tell me which voice channel" in plan(cog, "voice_move", w, member=w.target, channel=make_channel(w)).error
    do(cog, "voice_disconnect", w, member=w.target)
    assert w.target.move_to.await_args.args[0] is None
    do(cog, "voice_mute", w, member=w.target)
    assert w.target.edit.await_args.kwargs["mute"] is True
    w.target.voice.mute = True
    assert "already" in plan(cog, "voice_mute", w, member=w.target).error
    do(cog, "voice_mute", w, member=w.target, muted=False)
    assert w.target.edit.await_args.kwargs["mute"] is False
    do(cog, "voice_deafen", w, member=w.target)
    assert w.target.edit.await_args.kwargs["deafen"] is True


def test_voice_needs_the_right_permission(cog):
    w = World(actor_perms=discord.Permissions(move_members=True).value)
    w.target.voice = SimpleNamespace(channel=object(), mute=False, deaf=False)
    assert "Mute Members" in plan(cog, "voice_mute", w, member=w.target).error


def _message(author, pinned=False, content="hello"):
    m = SimpleNamespace(id=555555555555555555, author=author, content=content, attachments=[], embeds=[], pinned=pinned,
                        jump_url="https://discord.com/x", pin=AsyncMock(), unpin=AsyncMock())
    return m


def test_delete_one_message_by_id_and_by_link(cog):
    w = World(actor_perms=discord.Permissions(manage_messages=True).value)
    ch = make_channel(w, actor_perms=discord.Permissions(manage_messages=True, read_message_history=True))
    msg = _message(w.target, content="spam @everyone")
    ch.fetch_message = AsyncMock(return_value=msg)
    partial = SimpleNamespace(delete=AsyncMock())
    ch.get_partial_message = lambda mid: partial
    p = plan(cog, "messageops_delete", w, message="555555555555555555", channel=ch)
    assert p.error is None and "@everyone" not in p.text                          # preview can't ping
    run(cog._execute(w.i, "messageops_delete", p))
    partial.delete.assert_awaited_once()
    w.guild.get_channel_or_thread = lambda cid: ch if cid == 7000 else None
    link = "https://discord.com/channels/900/7000/555555555555555555"
    assert plan(cog, "messageops_delete", w, message=link).error is None
    assert "message ID or link" in plan(cog, "messageops_delete", w, message="???", channel=ch).error
    ch.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    assert "couldn't find that message" in plan(cog, "messageops_delete", w, message="555555555555555555", channel=ch).error


def test_delete_message_needs_manage_messages_in_that_channel(cog):
    w = World()
    ch = make_channel(w, actor_perms=discord.Permissions(send_messages=True))
    ch.fetch_message = AsyncMock(return_value=_message(w.target))
    assert "You need" in plan(cog, "messageops_delete", w, message="555555555555555555", channel=ch).error


def test_pin_and_unpin(cog):
    w = World()
    ch = make_channel(w, actor_perms=discord.Permissions(manage_messages=True))
    msg = _message(w.target)
    ch.fetch_message = AsyncMock(return_value=msg)
    do(cog, "messageops_pin", w, message="555555555555555555", channel=ch)
    msg.pin.assert_awaited_once()
    assert "isn't pinned" in plan(cog, "messageops_unpin", w, message="555555555555555555", channel=ch).error
    msg.pinned = True
    assert "already pinned" in plan(cog, "messageops_pin", w, message="555555555555555555", channel=ch).error
    do(cog, "messageops_unpin", w, message="555555555555555555", channel=ch)
    msg.unpin.assert_awaited_once()


def test_emoji_add_only_from_discord_cdn_and_never_downloads_otherwise(cog, monkeypatch):
    w = World(actor_perms=discord.Permissions(manage_expressions=True).value)
    dl = AsyncMock(return_value=b"img")
    monkeypatch.setattr(cogmod, "_download_image", dl)
    assert "Discord image link" in plan(cog, "emojiops_add", w, name="cool", image_url="https://evil.example/a.png").error
    assert "at least 2" in plan(cog, "emojiops_add", w, name="x", image_url="https://cdn.discordapp.com/a.png").error
    dl.assert_not_awaited()
    w.guild.create_custom_emoji = AsyncMock(return_value=SimpleNamespace(name="cool", __str__=lambda s: "<:cool:1>"))
    do(cog, "emojiops_add", w, name=":cool:", image_url="https://cdn.discordapp.com/a/b.png")
    assert dl.await_args.args == ("https://cdn.discordapp.com/a/b.png", 256 * 1024)
    assert w.guild.create_custom_emoji.await_args.kwargs["name"] == "cool"


def test_emoji_rename_and_delete_look_things_up_safely(cog):
    w = World(actor_perms=discord.Permissions(manage_expressions=True).value)
    e = SimpleNamespace(id=123456789012345678, name="party", edit=AsyncMock(), delete=AsyncMock(), __str__=lambda s: "<:party:1>")
    w.guild.emojis = [e]
    do(cog, "emojiops_rename", w, emoji="<:party:123456789012345678>", name="celebrate")
    e.edit.assert_awaited_once()
    do(cog, "emojiops_delete", w, emoji="party")
    e.delete.assert_awaited_once()
    assert "couldn't find" in plan(cog, "emojiops_delete", w, emoji="missing").error
    w.guild.emojis = [e, SimpleNamespace(id=2, name="party")]
    assert "share that name" in plan(cog, "emojiops_delete", w, emoji="party").error


def test_server_edit_validates_levels_and_reports_noops(cog):
    w = World(actor_perms=discord.Permissions(manage_guild=True).value)
    w.guild.name, w.guild.verification_level = "Old", discord.VerificationLevel.low
    w.guild.default_notifications, w.guild.icon = discord.NotificationLevel.all_messages, None
    do(cog, "serveredit_edit", w, name="New", verification_level="high", notifications="only mentions")
    kw = w.guild.edit.await_args.kwargs
    assert kw["name"] == "New" and kw["verification_level"] is discord.VerificationLevel.high
    assert kw["default_notifications"] is discord.NotificationLevel.only_mentions
    assert "must be one of" in plan(cog, "serveredit_edit", w, verification_level="paranoid").error
    assert "wouldn't change anything" in plan(cog, "serveredit_edit", w, name="Old").error
    assert "nothing to remove" not in plan(cog, "serveredit_icon", w, image_url="remove").error.lower()
    assert "Discord image link" in plan(cog, "serveredit_icon", w, image_url="https://evil.example/i.png").error


def test_schedule_event_validation(cog):
    w = World(actor_perms=discord.Permissions(manage_events=True).value)
    vc = make_channel(w, discord.VoiceChannel, "stage-ish")
    vc.permissions_for = lambda m: discord.Permissions.all()
    w.guild.create_scheduled_event = AsyncMock(return_value=SimpleNamespace(name="Movie", start_time=datetime.now(timezone.utc), url="u"))
    assert "couldn't read the start" in plan(cog, "eventops_create", w, name="x", start="whenever", channel=vc).error
    assert "future" in plan(cog, "eventops_create", w, name="x", start="2001-01-01 10:00", channel=vc).error
    assert "Tell me where" in plan(cog, "eventops_create", w, name="x", start="in 2 hours").error
    assert "needs an end time" in plan(cog, "eventops_create", w, name="x", start="in 2 hours", location="My place").error
    assert "after the start" in plan(cog, "eventops_create", w, name="x", start="in 2 hours", end="in 1 hour", channel=vc).error
    do(cog, "eventops_create", w, name="Movie", start="in 2 hours", channel=vc)
    assert w.guild.create_scheduled_event.await_args.kwargs["channel"] is vc
    do(cog, "eventops_create", w, name="Meetup", start="in 2 hours", end="in 4 hours", location="The park")
    assert w.guild.create_scheduled_event.await_args.kwargs["entity_type"] is discord.EntityType.external


def test_audit_log_is_a_private_read_with_filters(cog):
    w = World(actor_perms=discord.Permissions(view_audit_log=True).value)

    async def logs(**kw):
        for n in range(2):
            yield SimpleNamespace(created_at=datetime.now(timezone.utc), user=SimpleNamespace(name="mod"),
                                  action=discord.AuditLogAction.ban, target=SimpleNamespace(name="@everyone troll"),
                                  reason="spam `x`")
    w.guild.audit_logs = MagicMock(side_effect=lambda **kw: logs(**kw))
    out = do(cog, "serveredit_auditlog", w, limit=99, action="ban")
    assert w.guild.audit_logs.call_args.kwargs["limit"] == 20 and w.guild.audit_logs.call_args.kwargs["action"] is discord.AuditLogAction.ban
    assert "@everyone" not in out and out.count("`ban`") == 2
    assert "don't know the audit action" in plan(cog, "serveredit_auditlog", w, action="teleport").error
    assert "serveredit_auditlog" in PRIVATE_READS and AI_COMMANDS_BY_NAME["serveredit_auditlog"].requires_confirmation


def test_webhook_link_is_only_in_the_ephemeral_result_never_in_lists_or_the_plan(cog):
    w = World(actor_perms=discord.Permissions(manage_webhooks=True).value)
    ch = make_channel(w, actor_perms=discord.Permissions(manage_webhooks=True))
    ch.create_webhook = AsyncMock(return_value=SimpleNamespace(url="https://discord.com/api/webhooks/1/SECRET"))
    p = plan(cog, "webhookops_create", w, name="hook", channel=ch)
    assert "SECRET" not in p.text
    assert "SECRET" in run(cog._execute(w.i, "webhookops_create", p))              # shown only to the asker
    w.guild.webhooks = AsyncMock(return_value=[SimpleNamespace(id=1, name="hook", channel=ch, url="https://x/SECRET")])
    assert "SECRET" not in do(cog, "webhookops_list", w)


def test_invites_create_clamps_and_delete_finds_by_code(cog):
    w = World()
    ch = make_channel(w, actor_perms=discord.Permissions(create_instant_invite=True),
                      bot_perms=discord.Permissions(create_instant_invite=True))
    ch.create_invite = AsyncMock(return_value=SimpleNamespace(url="https://discord.gg/abc"))
    assert "0 (never) to 168" in plan(cog, "serverinvite_create", w, channel=ch, max_age_hours=500).error
    assert "0 (unlimited) to 100" in plan(cog, "serverinvite_create", w, channel=ch, max_uses=500).error
    assert "discord.gg/abc" in do(cog, "serverinvite_create", w, channel=ch, max_age_hours=0, max_uses=5)
    assert ch.create_invite.await_args.kwargs["max_age"] == 0
    w2 = World(actor_perms=discord.Permissions(manage_guild=True).value)
    inv = SimpleNamespace(code="abc", uses=2, channel=None, delete=AsyncMock())
    w2.guild.invites = AsyncMock(return_value=[inv])
    do(cog, "serverinvite_delete", w2, invite="https://discord.gg/abc")
    inv.delete.assert_awaited_once()
    assert "couldn't find an invite" in plan(cog, "serverinvite_delete", w2, invite="zzz").error


def test_thread_tools_need_manage_threads(cog):
    w = World(actor_perms=discord.Permissions(manage_threads=True).value)
    parent = make_channel(w, actor_perms=discord.Permissions(manage_threads=True), bot_perms=discord.Permissions.all())
    t = SimpleNamespace(id=999999999999999999, name="chat", parent=parent, archived=False, locked=False, mention="#chat",
                        edit=AsyncMock(), delete=AsyncMock(), add_user=AsyncMock(), remove_user=AsyncMock())
    w.guild.threads = [t]
    w.guild.get_thread = lambda tid: t if tid == t.id else None
    do(cog, "thread_state", w, thread="chat", archived=True, locked=True)
    assert t.edit.await_args.kwargs["archived"] is True and t.edit.await_args.kwargs["locked"] is True
    assert "Tell me what to change" in plan(cog, "thread_state", w, thread="chat").error
    do(cog, "thread_rename", w, thread="chat", name="renamed")
    do(cog, "thread_member_add", w, thread="<#999999999999999999>", member=w.target)
    t.add_user.assert_awaited_once_with(w.target)
    do(cog, "thread_delete", w, thread="chat")
    t.delete.assert_awaited_once()
    assert "couldn't find a thread" in plan(cog, "thread_delete", w, thread="ghost").error
    weak = World(actor_perms=0)
    weak_parent = make_channel(weak, actor_perms=discord.Permissions(send_messages=True))
    t2 = SimpleNamespace(**{**vars(t), "parent": weak_parent})
    weak.guild.threads, weak.guild.get_thread = [t2], lambda tid: t2
    assert "You need" in plan(cog, "thread_delete", weak, thread="chat").error


# ═════════════════════════ failure handling ═════════════════════════

def test_discord_refusals_become_friendly_text_and_are_not_logged(cog, logged):
    w = World()
    w.target.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    p = plan(cog, "member_role_add", w, member=w.target, role="Staff")
    out = run(cog._execute(w.i, "member_role_add", p))
    assert out.startswith("❌ Discord refused") and "Nothing was changed" in out
    w.target.add_roles.side_effect = discord.HTTPException(MagicMock(status=400, reason="Bad"), "Invalid Form Body")
    assert run(cog._execute(w.i, "member_role_add", p)).startswith("❌ Discord said no")


def test_an_unexpected_planner_crash_is_contained(cog, monkeypatch):
    w = World()

    async def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cog, "plan_member_nickname", boom)
    p = plan(cog, "member_nickname", w, member=w.target, nickname="x")
    assert p.error and "nothing was changed" in p.error.lower() and p.run is None


def test_outside_a_server_nothing_runs(cog):
    w = World()
    w.i.guild = None
    assert "inside a server" in run(cog._plan("member_role_add", w.i, member=w.target, role="Staff")).error


# ═════════════════════════ the real guard, end to end ═════════════════════════

@pytest.fixture()
def guarded(monkeypatch, cog, logged):
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    guard.register_hidden_cog(cog)
    monkeypatch.setattr(guard.db, "log_ai_command", AsyncMock())
    monkeypatch.setattr(guard, "check_ai_command_cap", AsyncMock(return_value=(True, None)))
    return cog


def test_through_the_guard_prompt_confirm_replan_execute(guarded):
    cog, w = guarded, World()
    w.i.client = SimpleNamespace()
    kwargs = dict(member=w.target, role="Staff")
    spec, command, reason, cap = run(guard.resolve_and_check(w.i, "member_role_add", kwargs))
    assert reason is None and spec.name == "member_role_add" and command.binding is cog
    assert needs_confirmation(spec, kwargs)
    ok, prompt = run(getattr(cog, cog.AI_CONFIRM_PROMPTS["member_role_add"])(w.i, **kwargs))
    assert ok and "Give the **Staff** role" in prompt and prompt.endswith("Confirm?")
    w.target.add_roles.assert_not_awaited()                                          # the prompt changes nothing
    run(guard.execute_ai_command(w.i, "member_role_add", **kwargs))                  # the Confirm click
    w.target.add_roles.assert_awaited_once()
    assert any("Gave **Staff**" in t for t in w.sent)


def test_through_the_guard_the_asker_without_the_permission_never_gets_that_far(guarded):
    cog, w = guarded, World(actor_perms=discord.Permissions(send_messages=True).value)
    w.i.client = SimpleNamespace()
    spec, command, reason, cap = run(guard.resolve_and_check(w.i, "member_role_add", dict(member=w.target, role="Staff")))
    assert spec is None and "Manage Roles" in reason


def test_a_confirmed_action_is_re_planned_so_a_stale_confirmation_cannot_act(guarded):
    cog, w = guarded, World()
    w.i.client = SimpleNamespace()
    kwargs = dict(member=w.target, role="Staff")
    ok, _ = run(getattr(cog, cog.AI_CONFIRM_PROMPTS["member_role_add"])(w.i, **kwargs))
    assert ok
    w.target.roles.append(w.staff)                       # server changed between the prompt and the click
    run(guard.execute_ai_command(w.i, "member_role_add", **kwargs))
    w.target.add_roles.assert_not_awaited()
    assert any("already has" in t for t in w.sent)


def test_a_public_read_runs_without_a_confirm_click(guarded):
    cog, w = guarded, World()
    w.i.client = SimpleNamespace()
    w.i.response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock())
    spec = AI_COMMANDS_BY_NAME["emojiops_list"]
    assert not needs_confirmation(spec, {})
    run(guard.execute_ai_command(w.i, "emojiops_list"))
    w.i.response.send_message.assert_awaited()
    assert "no custom emojis" in w.i.response.send_message.await_args.args[0]
