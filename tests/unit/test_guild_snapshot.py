"""Snapshot / restore (modules/guild_snapshot.py) against a fake guild: delete waves, edits, extras added by the test,
anti-nuke recreation with new ids, managed roles, failures, the gate, and the integrity check."""
import asyncio
import copy
from types import SimpleNamespace

import discord
import pytest

from modules import guild_snapshot as gs
from tests.unit._fake_guild import FakeGuild, FakeMember

PERM = discord.Permissions


def run(coro):
    return asyncio.run(coro)


async def nosleep(_):
    return None


def ow(allow=0, deny=0):
    return discord.PermissionOverwrite.from_pair(PERM(allow), PERM(deny))


def build():
    g = FakeGuild()
    g.members[555] = FakeMember(555)
    admin = g.add_role("Admin", permissions=8, color=0xFF0000, hoist=True)
    mod = g.add_role("Mod", permissions=PERM(kick_members=True).value)
    g.add_role("Member", mentionable=True)
    g.add_role("BotRole", managed=True)
    info = g.add_channel("INFO", "category")
    chat = g.add_channel("CHAT", "category")
    rules = g.add_channel("rules", "text", info, topic="be nice")
    ann = g.add_channel("news", "news", info)
    gen = g.add_channel("general", "text", chat, topic="hello", slowmode_delay=5)
    nsfw = g.add_channel("spicy", "text", chat, nsfw=True)
    g.add_channel("Lounge", "voice", chat, bitrate=64000, user_limit=10)
    g.add_channel("stage", "stage_voice", chat)
    g.add_channel("help", "forum", chat, topic="ask")
    g.add_channel("loose", "text")
    gen.overwrites[g.roles[0]] = ow(deny=PERM(send_messages=True).value)
    gen.overwrites[mod] = ow(allow=PERM(send_messages=True).value)
    gen.overwrites[g.members[555]] = ow(allow=PERM(view_channel=True).value)
    chat.overwrites[admin] = ow(allow=PERM(manage_messages=True).value)
    g.system_channel, g.rules_channel = gen, rules
    return g


def snap_of(g):
    return gs.seal(gs.capture(g))


def restore(g, snap, **kw):
    return run(gs.restore(g, snap, reason="t", pause=0, sleep=nosleep, **kw))


def by_name(g, name):
    return next(c for c in g.channels if c.name == name)


# ── snapshot integrity ───────────────────────────────────────────────────
def test_capture_is_deterministic_and_sealed():
    g = build()
    a, b = snap_of(g), snap_of(g)
    assert a == b and gs.verify(a)
    assert gs.summary(a) == "5 roles, 2 categories, 8 channels"


def test_tampered_or_wrong_version_snapshot_is_rejected():
    s = snap_of(build())
    t = copy.deepcopy(s)
    t["channels"][0]["name"] = "evil"
    assert not gs.verify(t)
    u = copy.deepcopy(s)
    u["version"] = 99
    assert not gs.verify(u) and not gs.verify({}) and not gs.verify(None)


def test_corrupt_snapshot_never_touches_the_server():
    g = build()
    s = snap_of(g)
    s["roles"][0]["name"] = "x"
    delete_me = g.add_channel("added", "text")
    res = restore(g, s)
    assert not res["ok"] and "integrity" in res["error"] and g.get_channel(delete_me.id)
    assert g.calls == []


# ── nothing to do ────────────────────────────────────────────────────────
def test_unchanged_server_plans_nothing_and_calls_nothing():
    g = build()
    s = snap_of(g)
    assert gs.plan_restore(s, gs.capture(g))["ops"] == []
    res = restore(g, s)
    assert res["ok"] and res["done"] == 0 and g.calls == []


# ── the attacks it has to undo ───────────────────────────────────────────
def test_channel_delete_wave_is_fully_restored_and_second_plan_is_empty():
    g = build()
    s = snap_of(g)
    for c in [c for c in g.channels if c.type.name != "category"]:
        run(c.delete())
    res = restore(g, s)
    assert res["ok"], res
    assert gs.plan_restore(s, gs.capture(g))["ops"] == []
    gen = by_name(g, "general")
    assert (gen.topic, gen.slowmode_delay) == ("hello", 5)
    assert gen.category_id == by_name(g, "CHAT").id
    assert by_name(g, "news").type.name == "news" and by_name(g, "stage").type.name == "stage_voice"
    assert (by_name(g, "Lounge").bitrate, by_name(g, "Lounge").user_limit) == (64000, 10)
    assert by_name(g, "spicy").nsfw and by_name(g, "loose").category_id is None


def test_overwrites_are_restored_and_remapped_to_recreated_roles():
    g = build()
    s = snap_of(g)
    mod_old = next(r for r in g.roles if r.name == "Mod").id
    for c in [c for c in g.channels if c.type.name != "category"]:
        run(c.delete())
    run(next(r for r in g.roles if r.name == "Mod").delete())
    res = restore(g, s)
    assert res["ok"], res
    new_mod = next(r for r in g.roles if r.name == "Mod")
    assert new_mod.id != mod_old
    gen = by_name(g, "general")
    assert g.roles[0] in gen.overwrites and new_mod in gen.overwrites and gen.overwrites[new_mod].pair()[0].send_messages
    assert g.members[555] in gen.overwrites


def test_deleted_category_is_recreated_and_its_channels_move_back():
    g = build()
    s = snap_of(g)
    run(by_name(g, "CHAT").delete())
    assert by_name(g, "general").category_id is None
    res = restore(g, s)
    assert res["ok"], res
    chat = by_name(g, "CHAT")
    assert by_name(g, "general").category_id == chat.id and by_name(g, "help").category_id == chat.id
    assert any(getattr(t, "name", "") == "Admin" for t in chat.overwrites)


def test_role_delete_wave_and_role_edits_are_reverted():
    g = build()
    s = snap_of(g)
    admin = next(r for r in g.roles if r.name == "Admin")
    run(next(r for r in g.roles if r.name == "Member").delete())
    run(admin.edit(name="pwned", permissions=PERM(administrator=True), colour=discord.Colour(1), hoist=False))
    g.roles[0].permissions = PERM(administrator=True)           # @everyone escalated
    res = restore(g, s)
    assert res["ok"], res
    assert admin.name == "Admin" and admin.color.value == 0xFF0000 and admin.hoist and admin.permissions.value == 8
    assert g.roles[0].permissions.value == 1024
    assert next(r for r in g.roles if r.name == "Member").mentionable


def test_everything_the_test_added_is_removed():
    g = build()
    s = snap_of(g)
    cat = g.add_channel("RAID", "category")
    for i in range(5):
        g.add_channel(f"spam{i}", "text", cat)
    g.add_channel("stray", "text", by_name(g, "CHAT"))
    g.add_role("raider1")
    res = restore(g, s)
    assert res["ok"], res
    assert not any(c.name.startswith(("spam", "RAID", "stray")) for c in g.channels) and not any(r.name == "raider1" for r in g.roles)
    assert [r.name for r in g.roles if r.managed] == ["BotRole"]


def test_edits_to_channels_and_settings_are_reverted():
    g = build()
    s = snap_of(g)
    gen = by_name(g, "general")
    run(gen.edit(name="hacked", topic="join evil.gg", slowmode_delay=0, nsfw=True))
    run(by_name(g, "rules").edit(category=by_name(g, "CHAT")))
    gen.overwrites.clear()
    g.verification_level = discord.VerificationLevel.none
    g.system_channel = None
    res = restore(g, s)
    assert res["ok"], res
    assert (gen.name, gen.topic, gen.slowmode_delay, gen.nsfw) == ("general", "hello", 5, False)
    assert by_name(g, "rules").category_id == by_name(g, "INFO").id
    assert len(gen.overwrites) == 3
    assert g.verification_level == discord.VerificationLevel.low and g.system_channel is gen


def test_reordered_channels_and_roles_are_put_back():
    g = build()
    s = snap_of(g)
    run(by_name(g, "stage").edit(position=0))
    run(by_name(g, "INFO").edit(position=1))
    a, b = next(r for r in g.roles if r.name == "Admin"), next(r for r in g.roles if r.name == "Member")
    a.position, b.position = b.position, a.position
    res = restore(g, s)
    assert res["ok"], res
    assert gs.plan_restore(s, gs.capture(g))["ops"] == []


# ── anti-nuke already recreated things (new ids) ─────────────────────────
def test_items_recreated_by_antinuke_are_matched_not_duplicated_or_deleted():
    g = build()
    s = snap_of(g)
    gen = by_name(g, "general")
    run(gen.delete())
    again = g.add_channel("general", "text", by_name(g, "CHAT"))        # anti-nuke's copy, new id, no topic
    g.calls.clear()
    res = restore(g, s)
    assert res["ok"], res
    assert [c.id for c in g.channels if c.name == "general"] == [again.id]
    assert again.topic == "hello" and again.slowmode_delay == 5 and len(again.overwrites) == 3
    assert not any(c[0] == "channel.delete" for c in g.calls)


# ── limits, notes and failures ───────────────────────────────────────────
def test_managed_roles_and_unsupported_types_are_reported_not_touched():
    g = build()
    g.add_channel("thread-hub", "private_thread_x")
    s = snap_of(g)
    run(next(r for r in g.roles if r.managed).delete())
    res = restore(g, s)
    assert any("BotRole" in n for n in res["notes"]) and any("unsupported type" in n for n in res["notes"])
    assert not any(c[0] == "create_role" and c[1] == "BotRole" for c in g.calls)


def test_a_failed_op_is_reported_and_the_rest_still_runs():
    g = build()
    s = snap_of(g)
    run(by_name(g, "general").delete())
    run(by_name(g, "help").delete())
    g.fail["create_channel"] = 1
    res = restore(g, s)
    assert not res["ok"] and len(res["failed"]) == 1 and "Missing Permissions" in res["failed"][0][1]
    assert res["done"] >= 1 and res["remaining"]
    assert res["skipped"] and all("was not restored" in why for _, why in res["skipped"])   # dependents skipped, not crashed


def test_five_failures_in_a_row_abort_the_run():
    g = build()
    s = snap_of(g)
    for c in [c for c in g.channels if c.type.name != "category"]:
        run(c.delete())
    g.fail["create_channel"] = -1                                       # every create is refused
    res = restore(g, s)
    assert res["aborted"] and len(res["failed"]) == 5 and not res["ok"]


def test_gate_is_rechecked_before_every_op_and_stops_the_run():
    g = build()
    s = snap_of(g)
    for c in [c for c in g.channels if c.type.name != "category"]:
        run(c.delete())
    left = {"n": 3}

    def allowed():
        left["n"] -= 1
        return left["n"] >= 0
    res = restore(g, s, still_allowed=allowed)
    assert res["aborted"] and res["done"] == 3 and any("gate" in f[0] for f in res["failed"])


def test_pause_between_every_discord_call():
    g = build()
    s = snap_of(g)
    run(by_name(g, "general").delete())
    sleeps = []

    async def rec(t):
        sleeps.append(t)
    res = run(gs.restore(g, s, reason="t", pause=0.7, sleep=rec))
    assert res["ok"] and sleeps and set(sleeps) == {0.7} and len(sleeps) == res["done"] + len(res["failed"])
