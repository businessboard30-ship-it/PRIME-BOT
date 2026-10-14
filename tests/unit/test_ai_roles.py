"""AI role commands: create one role, bulk-create, bulk-delete -- cosmetic-only roles, capped lists, hierarchy-safe
deletes, and the allowlist entries that let /aichat run them."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import ai_bulk
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME, is_command_allowed, needs_confirmation


def run(coro):
    return asyncio.run(coro)


# ── pure planning ─────────────────────────────────────────────────────────

def test_parse_color_accepts_names_hex_and_rejects_junk():
    assert ai_bulk.parse_color("red") == 0xED4245
    assert ai_bulk.parse_color("#FF8800") == 0xFF8800
    assert ai_bulk.parse_color("0xff8800") == 0xFF8800
    assert ai_bulk.parse_color("f80") == 0xFF8800
    assert ai_bulk.parse_color("") is None and ai_bulk.parse_color(None) is None
    assert ai_bulk.parse_color("not-a-colour") == -1


def test_role_name_strips_mentions_and_clamps():
    assert ai_bulk.role_name("  @everyone   VIP ") == "everyone VIP"
    assert len(ai_bulk.role_name("x" * 300)) == 100
    assert ai_bulk.role_name("Game  Night") == "Game Night"


def test_plan_role_creation_caps_dedupes_and_blocks_everyone():
    names = ",".join(f"r{i}" for i in range(20)) + ",Member,@everyone,here"
    plan = ai_bulk.plan_role_creation(names, existing=["member"])
    assert len(plan["create"]) == ai_bulk.MAX_CREATE_ROLES
    assert plan["duplicates"] == ["Member"]
    assert plan["invalid"] == ["@everyone", "here"]
    assert len(plan["over_cap"]) == 20 - ai_bulk.MAX_CREATE_ROLES
    assert ai_bulk.plan_role_creation("a, A, a", [])["create"] == ["a"]


def test_plan_role_creation_respects_servers_remaining_room():
    plan = ai_bulk.plan_role_creation("a,b,c,d", [], room=2)
    assert plan["create"] == ["a", "b"] and plan["over_cap"] == ["c", "d"]
    assert ai_bulk.plan_role_creation("a", [], room=0)["create"] == []


# ── allowlist ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["role_create", "role_bulk_create", "role_bulk_delete"])
def test_role_commands_are_allowed_confirmed_and_permission_gated(name):
    spec = is_command_allowed(name)
    assert spec is not None
    assert spec.cog_module == "discord_bot.cogs.role_setup"
    assert needs_confirmation(spec, {})
    assert spec.needs_perms == ("manage_roles",)
    assert spec.command_path == name.replace("role_bulk_", "role bulk-").replace("role_create", "role create")


def test_role_cog_declares_handlers_prompts_and_params_for_every_ai_role_command():
    from discord_bot.cogs.role_setup import RoleSetupCog
    for tool in ("role_create", "role_bulk_create", "role_bulk_delete"):
        assert hasattr(RoleSetupCog, RoleSetupCog.AI_CONFIRMED_HANDLERS[tool])
        assert hasattr(RoleSetupCog, RoleSetupCog.AI_CONFIRM_PROMPTS[tool])
    assert set(RoleSetupCog.AI_TOOL_PARAMS) == {"create", "bulk-create", "bulk-delete"}
    assert {c.name for c in RoleSetupCog.group.commands} >= {"setup", "create", "bulk-create", "bulk-delete"}


# ── Discord-side behaviour (mocked guild) ─────────────────────────────────

def _role(rid, name, pos, managed=False, default=False):
    r = MagicMock(spec=discord.Role)
    r.id, r.name, r.position, r.managed = rid, name, pos, managed
    r.is_default.return_value = default
    r.__ge__ = lambda self, other: self.position >= other.position
    r.__lt__ = lambda self, other: self.position < other.position
    r.delete = AsyncMock()
    r.mention = f"<@&{rid}>"
    return r


def _guild(roles, bot_top, owner_id=999):
    g = MagicMock(spec=discord.Guild)
    g.id, g.owner_id, g.roles = 1, owner_id, roles
    g.me = SimpleNamespace(top_role=bot_top, guild_permissions=discord.Permissions(manage_roles=True))
    g.default_role = next((r for r in roles if r.is_default()), None)
    g.get_role = lambda i: next((r for r in roles if r.id == i), None)
    return g


def _asker(top, uid=5, manage_roles=True):
    return SimpleNamespace(id=uid, top_role=top, guild_permissions=discord.Permissions(manage_roles=manage_roles))


def test_resolve_roles_protects_everyone_managed_and_higher_roles():
    from discord_bot.cogs import _ai_bulk_ops as ops
    everyone, low, mid = _role(1, "@everyone", 0, default=True), _role(2, "Low", 1), _role(3, "Mid", 5)
    bot_role, botmanaged, high = _role(4, "BotTop", 8), _role(5, "Integration", 2, managed=True), _role(6, "High", 9)
    g = _guild([everyone, low, mid, bot_role, botmanaged, high], bot_top=bot_role)
    res = ops.resolve_roles(g, ["Low", "Mid", "@everyone", "Integration", "High", "Nope"], _asker(mid))
    assert [r.name for r in res["found"]] == ["Low"]          # Mid is the asker's own top role -> protected too
    why = dict((r.name, w) for r, w in res["protected"])
    assert "everyone" in why["@everyone"] and "bot or integration" in why["Integration"]
    assert "above my own" in why["High"] and "your own top role" in why["Mid"]
    assert res["missing"] == ["Nope"]


def test_resolve_roles_server_owner_may_delete_up_to_the_bots_top_role():
    from discord_bot.cogs import _ai_bulk_ops as ops
    a, b, bot_role = _role(2, "A", 5), _role(3, "B", 6), _role(4, "BotTop", 8)
    g = _guild([a, b, bot_role], bot_top=bot_role, owner_id=5)
    res = ops.resolve_roles(g, ["A", "B"], _asker(_role(9, "x", 0), uid=5))
    assert [r.name for r in res["found"]] == ["A", "B"]


def test_resolve_roles_reports_ambiguous_names_instead_of_guessing():
    from discord_bot.cogs import _ai_bulk_ops as ops
    r1, r2, bot_role = _role(2, "Dup", 1), _role(3, "dup", 2), _role(4, "BotTop", 8)
    g = _guild([r1, r2, bot_role], bot_top=bot_role)
    res = ops.resolve_roles(g, ["dup"], _asker(_role(9, "Top", 7)))
    assert res["found"] == [] and res["ambiguous"] == ["dup"]


def test_create_roles_makes_permissionless_roles_one_at_a_time(monkeypatch):
    from discord_bot.cogs import _ai_bulk_ops as ops
    monkeypatch.setattr(ai_bulk, "PAUSE_SECONDS", 0)
    calls = []

    async def create_role(**kw):
        calls.append(kw)
        return SimpleNamespace(name=kw["name"])
    g = SimpleNamespace(create_role=create_role)
    made, stop = run(ops.create_roles(g, ["A", "B", "C"], 0xFF0000, True, False, reason="t"))
    assert [m.name for m in made] == ["A", "B", "C"] and stop is None
    for kw in calls:
        assert kw["permissions"] == discord.Permissions.none()       # never any permissions, never Administrator
        assert kw["colour"] == discord.Colour(0xFF0000) and kw["hoist"] is True and kw["mentionable"] is False


def test_create_roles_stops_at_first_failure_instead_of_retrying(monkeypatch):
    from discord_bot.cogs import _ai_bulk_ops as ops
    monkeypatch.setattr(ai_bulk, "PAUSE_SECONDS", 0)
    n = {"i": 0}

    async def create_role(**kw):
        n["i"] += 1
        if n["i"] == 2:
            raise discord.Forbidden(MagicMock(status=403), "no")
        return SimpleNamespace(name=kw["name"])
    made, stop = run(ops.create_roles(SimpleNamespace(create_role=create_role), ["A", "B", "C"], None, False, False, "t"))
    assert [m.name for m in made] == ["A"] and "Manage Roles" in stop and n["i"] == 2


def test_role_create_plan_checks_permissions_colour_and_duplicates():
    from discord_bot.cogs.role_setup import RoleSetupCog
    cog = RoleSetupCog(bot=MagicMock())
    existing = [_role(1, "@everyone", 0, default=True), _role(2, "Member", 1)]
    g = _guild(existing, bot_top=existing[1])
    inter = SimpleNamespace(guild=g, user=_asker(existing[1]))
    err, plan = run(cog._role_create_plan(inter, "Member, VIP, Staff", "blue", True, None))
    assert err is None and plan["create"] == ["VIP", "Staff"] and plan["duplicates"] == ["Member"]
    assert plan["colour"] == 0x3498DB and plan["hoist"] is True and plan["mentionable"] is False
    err, _ = run(cog._role_create_plan(inter, "VIP", "not-a-colour"))
    assert "colour" in err
    err, _ = run(cog._role_create_plan(inter, "Member"))
    assert "Nothing to create" in err and "already exist" in err
    noperm = SimpleNamespace(guild=g, user=_asker(existing[1], manage_roles=False))
    err, _ = run(cog._role_create_plan(noperm, "VIP"))
    assert "Manage Roles" in err


def test_single_role_name_with_a_comma_is_kept_whole_for_role_create():
    from discord_bot.cogs.role_setup import RoleSetupCog
    assert RoleSetupCog._names_arg(None, "Smith, John") == ["Smith, John"]
    assert RoleSetupCog._names_arg("a, b", None) == "a, b"


def test_role_delete_plan_caps_the_list_and_needs_permission():
    from discord_bot.cogs.role_setup import RoleSetupCog
    cog = RoleSetupCog(bot=MagicMock())
    bot_role = _role(4, "BotTop", 20)
    roles = [_role(10 + i, f"r{i}", 1 + i) for i in range(12)] + [bot_role]
    g = _guild(roles, bot_top=bot_role)
    inter = SimpleNamespace(guild=g, user=_asker(_role(99, "Top", 19)))
    err, _ = run(cog._role_delete_plan(inter, ",".join(f"r{i}" for i in range(12))))
    assert str(ai_bulk.MAX_DELETE_ROLES) in err
    err, res = run(cog._role_delete_plan(inter, "r0, r1"))
    assert err is None and [r.name for r in res["found"]] == ["r0", "r1"]
    err, _ = run(cog._role_delete_plan(SimpleNamespace(guild=g, user=_asker(_role(99, "T", 19), manage_roles=False)), "r0"))
    assert "Manage Roles" in err
