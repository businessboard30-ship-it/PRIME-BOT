"""AI chat sends only the relevant command tools (Groq 8k tokens/min cap)."""
import json

from modules.ai_tool_select import select_tools, MAX_TOOLS, MAX_TOOL_TOKENS


def tool(name, desc, **params):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": "string", "description": v} for k, v in params.items()}, "required": []}}}


TOOLS = [tool(f"filler_{i}", f"does unrelated thing number {i}") for i in range(100)] + [
    tool("level", "Show your level card and XP", user="member"),
    tool("ban", "Ban a member from the server", user="member", reason="why"),
    tool("welcome_setup", "Set up welcome messages"),
    tool("balance", "Check your coin balance"),
]


def names(ts):
    return [t["function"]["name"] for t in ts]


def test_picks_matching_commands_first():
    assert names(select_tools(TOOLS, "what's my level?"))[0] == "level"
    assert "ban" in names(select_tools(TOOLS, "ban that guy for spam"))
    assert names(select_tools(TOOLS, "set up the welcome message"))[0] == "welcome_setup"


def test_stemming_matches_variants():
    assert "level" in names(select_tools(TOOLS, "show my levels please"))
    assert "ban" in names(select_tools(TOOLS, "he got banned"))


def test_no_match_or_empty_means_plain_chat():
    assert select_tools(TOOLS, "tell me a joke about cats") == []
    assert select_tools(TOOLS, "") == []
    assert select_tools([], "level") == []


def test_capped_and_within_token_budget():
    many = [tool(f"level_{i}", "level xp card " + "x" * 400) for i in range(60)]
    out = select_tools(many, "level xp card")
    assert 0 < len(out) <= MAX_TOOLS
    assert len(json.dumps(out)) // 4 <= MAX_TOOL_TOKENS + len(json.dumps(out[0])) // 4
