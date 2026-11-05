"""RULE: anything wired into /aichat must also be wired into replying to the bot and @mentioning it (and the other way round).
Both paths are separate code (the slash command + _run_chat_turn, and on_bot_chat + _run_reply_turn), so this test fails the moment
one gets a step the other lacks. If you add a step to one, add it to the other, or list it in INTENTIONALLY_DIFFERENT with why."""
import re

SRC = open("discord_bot/cogs/ai_tools.py").read()


def body(name: str) -> str:
    m = re.search(rf"    (?:async )?def {name}\(.*?(?=\n    (?:@|async def |def )|\Z)", SRC, re.S)
    assert m, name
    return m.group(0)


SLASH = body("aichat") + body("_run_chat_turn")
REPLY = body("on_bot_chat") + body("_run_reply_turn")

# every step both paths must have
SHARED = ["mentions_other_bot(", "_quick_answer(", "_prefs_quick_answer(", "_memory_quick_answer(", "is_capabilities_question(",
          "_capabilities_view(", "_build_command_tools(", "select_tools_multi(", "_command_context(", "_with_choice_tool(",
          "ai_chat(", "_run_tool_calls(", "_second_turn(", "_voice_setting(", "build_attachment_context(", "extract_support_marker(",
          "strip_dashboard_marker(", "hide_long_reply(", "send_with_fallbacks(", "guild_id="]

INTENTIONALLY_DIFFERENT = {
    "check_ai_usage_limit(": "slash uses the per-user tier cap; replies use the per-server reply cap (check_reply_limit)",
    "check_reply_limit(": "see above",
    "get_or_create_active_session(": "slash keeps a session; replies use the reply chain as context",
    "tools_allowed(": "replies withhold tools when the chain holds other people's text (slash input is only the caller's)",
}


def test_every_shared_step_is_in_both_paths():
    missing = [(step, "slash" if step not in SLASH else "reply") for step in SHARED if step not in SLASH or step not in REPLY]
    assert not missing, f"wired into only one path: {missing}"


def test_known_differences_are_still_one_sided_on_purpose():
    for step in INTENTIONALLY_DIFFERENT:
        assert (step in SLASH) != (step in REPLY), f"{step} is now on both or neither path; update INTENTIONALLY_DIFFERENT"


def test_both_paths_pass_the_server_to_the_model():
    assert "guild_id=getattr(guild" in body("_run_chat_turn") and "guild_id=guild_id" in body("_run_reply_turn")
