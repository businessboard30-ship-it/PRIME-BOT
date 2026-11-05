"""One extra model turn for multi-step requests.

Problem: the model often calls only the FIRST tool (a lookup) and stops, and the bot used to give it no second turn, so the
rest of "check stats, lock #general, schedule X, reopen" never even reached the Confirm button. After the first tool calls ran,
we ask the model ONCE more for the remaining steps. Pure helpers here; the loop lives in discord_bot/cogs/ai_tools.py.
Safety is unchanged: whatever comes back goes down the normal path (allowlist, real permission, daily cap, Confirm, plan runner).
"""
from typing import Dict, List

from modules.ai_tool_select import split_clauses

MIN_CLAUSES = 3          # "a, b and c" -- a one- or two-part request doesn't need a second turn


def needs_followup(message: str, calls: List[Dict]) -> bool:
    """True when the model made ONE call for a request with several parts."""
    return len([c for c in (calls or []) if isinstance(c, dict)]) == 1 and len(split_clauses(message)) >= MIN_CLAUSES


def done_names(calls: List[Dict]) -> List[str]:
    return [str(c.get("name")) for c in (calls or []) if isinstance(c, dict) and c.get("name")]


def followup_prompt(message: str, done: List[str]) -> str:
    return (f"Original request: {message}\n\n"
            f"Already handled in your last reply (do NOT call these again): {', '.join(done) or 'nothing'}.\n"
            "Now call EVERY other tool this request still needs, in the order asked, all in this one reply. "
            "If a remaining step has no tool, skip it. Don't write a plan or ask a question; just make the calls.")


def remaining_calls(calls: List[Dict], done: List[str]) -> List[Dict]:
    """The second reply's tool calls minus anything already run."""
    return [c for c in (calls or []) if isinstance(c, dict) and c.get("name") and c.get("name") not in set(done)]
