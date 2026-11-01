"""Phase 5 skills: registration, input validation, unauthorised use, no escape from the allowlist, wiring."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import ai_skills as sk
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME

ROOT = Path(__file__).resolve().parents[2]


def perms(**on):
    names = ("manage_channels", "manage_roles", "manage_guild", "administrator")
    return SimpleNamespace(**{n: on.get(n, False) for n in names})


ALL = perms(manage_channels=True, manage_roles=True, manage_guild=True)


def skill(name):
    return sk.get_skill(name)


def test_builtin_skills_registered_and_schema_shape():
    names = {s.name for s in sk.all_skills()}
    assert {"lockdown_channel", "reopen_channel", "welcome_quickstart"} <= names
    t = sk.tool_schema(skill("welcome_quickstart"))
    assert t["function"]["name"] == "skill__welcome_quickstart"
    assert set(t["function"]["parameters"]["required"]) == {"channel", "message"}


def test_every_builtin_skill_expands_only_to_allowlisted_commands():
    samples = {"lockdown_channel": {"channel": "#general", "reason": "raid"}, "reopen_channel": {},
               "welcome_quickstart": {"channel": "#welcome", "message": "Hi {user}"}}
    for s in sk.all_skills():
        if s.name not in samples:
            continue
        calls, err = sk.expand(s, samples[s.name], ALL)
        assert calls and not err and len(calls) <= sk.MAX_SKILL_STEPS
        assert all(c["name"] in AI_COMMANDS_BY_NAME and not sk.is_skill_call(c["name"]) for c in calls)


def test_lockdown_expansion_content():
    calls, _ = sk.expand(skill("lockdown_channel"), {"channel": "#general", "reason": "raid"}, ALL)
    assert [c["name"] for c in calls] == ["channeledit_lock", "channeledit_topic"]
    assert calls[1]["arguments"]["topic"].endswith("Locked: raid")
    calls, _ = sk.expand(skill("lockdown_channel"), {}, ALL)                    # defaults
    assert calls[0]["arguments"]["channel"] == "this"


@pytest.mark.parametrize("bad", [
    {"channel": "#w"},                                                           # missing message
    {"channel": "#w", "message": "x" * 251},                                     # too long
    {"channel": {"a": 1}, "message": "hi"},                                      # wrong type
    {"channel": True, "message": "hi"},
    "not a dict", None, [],
])
def test_invalid_inputs_are_rejected(bad):
    calls, err = sk.expand(skill("welcome_quickstart"), bad, ALL)
    assert calls is None and err


def test_unknown_inputs_dropped_and_control_chars_cleaned():
    clean, err = sk.validate_inputs(skill("welcome_quickstart"),
                                    {"channel": "#w", "message": "hi\u202e\nthere", "admin": True, "command": "ban"})
    assert not err and clean == {"channel": "#w", "message": "hi there"}


def test_unauthorised_user_cannot_use_a_skill():
    for who in (perms(), perms(manage_channels=True), perms(manage_roles=True)):
        calls, err = sk.expand(skill("lockdown_channel"), {"channel": "#g"}, who)
        assert calls is None and "permission" in err
    calls, err = sk.expand(skill("welcome_quickstart"), {"channel": "#w", "message": "hi"}, perms(manage_channels=True))
    assert calls is None


def test_tools_for_only_lists_skills_the_person_may_use(monkeypatch):
    async def none():
        return {}
    monkeypatch.setattr(sk, "overrides", lambda g: none())
    names = lambda ts: {t["function"]["name"] for t in ts}
    assert names(asyncio.run(sk.tools_for(1, perms()))) == set()
    assert names(asyncio.run(sk.tools_for(1, perms(manage_guild=True)))) == {"skill__welcome_quickstart"}
    assert "skill__lockdown_channel" in names(asyncio.run(sk.tools_for(1, ALL)))


def test_disabled_skill_is_hidden_and_is_enabled_respects_override(monkeypatch):
    async def ov():
        return {"lockdown_channel": False}
    monkeypatch.setattr(sk, "overrides", lambda g: ov())
    assert "skill__lockdown_channel" not in {t["function"]["name"] for t in asyncio.run(sk.tools_for(1, ALL))}
    assert asyncio.run(sk.is_enabled(1, skill("lockdown_channel"))) is False
    assert asyncio.run(sk.is_enabled(1, skill("reopen_channel"))) is True


def test_settings_failure_falls_back_to_defaults(monkeypatch):
    async def boom():
        raise RuntimeError("db")
    monkeypatch.setattr(sk, "_pool", boom)
    sk._cache.clear()
    assert asyncio.run(sk.overrides(5)) == {}


def test_cooldown():
    s = skill("reopen_channel")
    sk._last_used.clear()
    assert sk.check_cooldown(1, s, now=100.0) == (True, 0)
    sk.mark_used(1, s, now=100.0)
    ok, wait = sk.check_cooldown(1, s, now=105.0)
    assert not ok and wait >= 1
    assert sk.check_cooldown(1, s, now=100.0 + s.cooldown_seconds + 1)[0]
    assert sk.check_cooldown(2, s, now=105.0)[0]                                 # per person


def test_registration_rules_and_a_rogue_skill_cannot_escape_the_allowlist():
    with pytest.raises(ValueError):
        sk.register_skill(sk.Skill("lockdown_channel", "dup", (), lambda i: []))
    with pytest.raises(ValueError):
        sk.register_skill(sk.Skill("Bad Name", "x", (), lambda i: []))
    with pytest.raises(ValueError):
        sk.register_skill(sk.Skill("toomany", "x", (), lambda i: [], max_steps=99))
    bad = lambda call: sk.Skill("rogue_tmp", "x", (), lambda i: call)
    for call in ([{"name": "definitely_not_a_command", "arguments": {}}],
                 [{"name": "skill__lockdown_channel", "arguments": {}}],           # no recursion
                 [{"name": "channeledit_lock", "arguments": "x"}],
                 [{"name": "channeledit_lock", "arguments": {}}] * (sk.MAX_SKILL_STEPS + 1), []):
        assert sk.expand(bad(call), {}, ALL)[0] is None
    crash = sk.Skill("crash_tmp", "x", (), lambda i: 1 / 0)
    assert sk.expand(crash, {}, ALL)[0] is None


def test_skills_cannot_be_driven_by_the_ai_toggle_and_permission_guard_ignores_them():
    from modules.ai_command_allowlist import AI_COMMANDS
    assert not [c for c in AI_COMMANDS if "skill" in c.name]
    for f in ("modules/ai_command_guard.py", "modules/ai_command_allowlist.py"):
        assert "ai_skills" not in (ROOT / f).read_text()


def test_wiring():
    tools = (ROOT / "discord_bot/cogs/ai_tools.py").read_text()
    assert "_expand_skill_calls(ctx, calls)" in tools and "ai_skills.tools_for" in tools
    assert tools.index("_expand_skill_calls(ctx, calls)") < tools.index("ai_bulk.pick_tool_calls(calls)")
