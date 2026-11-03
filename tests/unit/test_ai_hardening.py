"""Phase 7 hardening: secrets never travel through the AI; other people's text never drives commands; audit + regression locks."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import ai_redact, ai_trust, ai_agents

ROOT = Path(__file__).resolve().parents[2]
SRC = lambda f: (ROOT / f).read_text()

# Built from fragments at runtime so no credential-shaped literal is ever committed (GitHub push protection).
DISCORD_TOKEN = "".join(["MTIzNDU2Nzg5MDEyMzQ1", "Njc4", ".", "GabcDE", ".", "abcdefghijklmnopqrstuvwxyz", "0123456"])
SAMPLES = {
    "discord": DISCORD_TOKEN,
    "github": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "github_pat": "github_pat_" + "A" * 50,
    "groq": "gsk_" + "x9Y8z7W6v5U4t3S2r1Q0p9O8",
    "openai": "sk-" + "abcdEFGH1234ijklMNOP5678",
    "aws": "AKI" + "A" + "IOSFODNN7EXAMPLE",
    "stripe": "sk_live_" + "abcdefghijklmnop1234",
    "fernet": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MDEyMzQ1Njc4OTA=",
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_credential_shapes_are_redacted(name):
    out = ai_redact.redact(f"here you go {SAMPLES[name]} thanks")
    assert SAMPLES[name] not in out and ai_redact.MASK in out and out.startswith("here you go")


def test_url_passwords_and_bearer_tokens_are_redacted_but_plain_links_are_not():
    out = ai_redact.redact("db postgresql://admin:hunter2@db.example.com:5432/x and Authorization: Bearer abcdefghijklmnop1234")
    assert "hunter2" not in out and "abcdefghijklmnop1234" not in out and "postgresql://" in out
    plain = "see https://example.com/page?x=1 and discord.gg/abc, call me at 5pm, my id is 123456789012345678"
    assert ai_redact.redact(plain) == plain


def test_own_secret_values_from_the_environment_are_redacted(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_API_KEY", "plainlooking-but-secret-value-77")
    monkeypatch.setenv("APP_NAME", "not-a-secret-name-value")
    monkeypatch.setenv("SHORT_TOKEN", "abc")
    out = ai_redact.redact("the key is plainlooking-but-secret-value-77, app not-a-secret-name-value, abc")
    assert "plainlooking" not in out and "not-a-secret-name-value" in out and out.endswith("abc")


def test_redact_is_safe_on_odd_input():
    for v in (None, 5, "", "short", ["x"], {"a": 1}):
        assert ai_redact.redact(v) == v
    assert not ai_redact.contains_secret("hello world, nothing here") and ai_redact.contains_secret(DISCORD_TOKEN)


def test_agent_task_text_is_redacted_before_it_reaches_the_model_or_the_database():
    task, _ = ai_agents.clean_task({"task": f"check stats, my key is {SAMPLES['github']}"})
    assert SAMPLES["github"] not in task and ai_redact.MASK in task


def test_ai_chat_redacts_what_goes_in_and_what_comes_out():
    s = SRC("modules/ai_features.py")
    assert "message = _redact(message)" in s and "response_text = _redact(response_text)" in s
    assert s.index("message = _redact(message)") < s.index("if not GROQ_API_KEYS:")
    assert s.index("response_text = _redact(response_text)") < s.index("if mentions_other_bot(response_text):")


# ---- trust rule: only the asker's own words can drive commands ---------------------------------------------------------

def hist(*pairs):
    msgs = [{"role": r, "content": "x"} for r, _ in pairs]
    return ai_trust.ChatHistory(msgs, {a for r, a in pairs if r == "user"})


def test_tools_only_when_every_human_message_is_the_askers():
    ASKER, STRANGER = 10, 20
    assert ai_trust.tools_allowed(hist(("user", ASKER), ("assistant", 0), ("user", ASKER)), ASKER)
    assert ai_trust.tools_allowed(hist(("assistant", 0)), ASKER) and ai_trust.tools_allowed([], ASKER)
    assert not ai_trust.tools_allowed(hist(("user", STRANGER), ("assistant", 0)), ASKER)           # a stranger's message in context
    assert not ai_trust.tools_allowed(hist(("user", ASKER), ("user", STRANGER)), ASKER)
    assert not ai_trust.tools_allowed([{"role": "user", "content": "unknown author"}], ASKER)      # provenance unknown -> closed
    assert ai_trust.tools_allowed([{"role": "assistant", "content": "bot only"}], ASKER)


def test_the_switch_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(ai_trust, "REPLY_TOOLS_REQUIRE_OWN_CONTEXT", False)
    assert ai_trust.tools_allowed(hist(("user", 20)), 10)


def test_reply_and_dm_history_builders_record_authors_and_the_reply_turn_withholds_tools():
    t = SRC("discord_bot/cogs/ai_tools.py")
    assert t.count("ai_trust.ChatHistory(") == 2 and "ai_trust.tools_allowed(history, message.author.id)" in t
    i = t.index("trusted = ai_trust.tools_allowed")
    assert "if (proxy is not None and trusted)" in t[i:i + 700]


def test_reply_turn_builds_no_command_tools_when_a_stranger_is_in_the_context():
    from discord_bot.cogs import ai_tools
    c = ai_tools.AIToolsCog.__new__(ai_tools.AIToolsCog)
    built, seen = [], {}

    async def build(proxy):
        built.append(1)
        return [{"function": {"name": "rank"}}]

    async def noop(*a, **k):
        return None

    async def ctxs(*a, **k):
        return "ctx"

    async def fake_chat(*a, **k):
        seen["tools"] = k.get("tools")
        return "plain text"

    async def vsetting(*a, **k):
        return None
    c.bot = SimpleNamespace(user=SimpleNamespace(id=1))
    c._build_command_tools, c._command_context, c._voice_setting = build, ctxs, vsetting
    c._quick_answer = lambda *a, **k: None
    c._prefs_quick_answer = c._memory_quick_answer = noop
    c._is_premium = noop
    c._with_choice_tool = lambda tools, cc, d: (tools, cc)
    msg = SimpleNamespace(author=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_guild=False)),
                          guild=SimpleNamespace(id=5), channel=SimpleNamespace(permissions_for=lambda a: None), attachments=[])
    import unittest.mock as m
    with m.patch.object(ai_tools, "ai_chat", fake_chat), m.patch.object(ai_tools, "ProxyInteraction", lambda *a: object()), \
            m.patch.object(ai_tools, "check_reply_limit", lambda *a: _ok()), m.patch.object(ai_tools, "_message_attachments", noop), \
            m.patch.object(ai_tools, "mentions_other_bot", lambda t: False):
        asyncio.run(c._run_reply_turn(msg, "hello", hist(("user", 20), ("assistant", 0))))
        assert built == [] and not seen["tools"]                                     # stranger in context: no tools built at all
        asyncio.run(c._run_reply_turn(msg, "hello", hist(("user", 10), ("assistant", 0))))
        assert built == [1]                                                          # only the asker: tools are built as before


async def _ok():
    return True, ""


# ---- audit + regression locks on protections that already exist -------------------------------------------------------

def test_scam_lookup_is_audited_allowed_and_denied():
    t = SRC("discord_bot/cogs/ai_tools.py")
    assert "_audit_lookup(guild, ctx, args, allowed=False" in t and "_audit_lookup(guild, ctx, {\"user_id\": target_id}, allowed=True)" in t
    assert "log_ai_command" in t[t.index("async def _audit_lookup"):t.index("async def _handle_scam_lookup")]


def test_regression_untrusted_content_never_gets_command_tools():
    f = SRC("modules/ai_features.py")
    assert "tools = None          # page / file content never gets command tools" in f      # fetched web pages
    t = SRC("discord_bot/cogs/ai_tools.py")
    assert "tools=None if atts else (tools or None)" in t                                  # reply path with attachments
    assert "not attachments" in t                                                          # /aichat with a file
    assert "if m.author.bot and m.author.id != self.bot.user.id:" in t and "return None" in t   # other bots / webhooks dropped


def test_regression_the_ai_cannot_run_its_own_control_surfaces():
    from modules.ai_command_allowlist import AI_COMMANDS
    for bad in ("aiskills", "aiagents", "agent", "skill", "aimemory_clear_all", "verify", "setowner", "eval"):
        assert not [c for c in AI_COMMANDS if bad in c.name], bad


def test_no_credentials_in_the_repo_sources():
    import re
    pat = re.compile(r"ghp_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
                     r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{30,}")
    bad = []
    for d in ("modules", "discord_bot", "database", "handlers", "utils"):
        for p in (ROOT / d).rglob("*.py"):
            if pat.search(p.read_text(errors="ignore")):
                bad.append(str(p.relative_to(ROOT)))
    assert not bad, bad
