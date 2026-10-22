"""The AI's "ask the person to choose" tool, drawn as a Components V2 panel.

It is NOT a Discord command and runs nothing. The model calls `ask_user_choice` when something is unclear (which of
several members/channels/roles, what amount, which option) instead of guessing. The panel's buttons/menus only hand the
person's pick BACK to the AI as its next message; anything that changes the server still goes through the normal
allowlist + permission + daily-cap + Confirm path. So a panel can never do more than the person could already ask for.
"""
from typing import Any, Dict, List, Optional

CHOICE_TOOL_NAME = "ask_user_choice"
MAX_CHOICE_DEPTH = 3          # chained questions in one conversation turn; after that the AI must decide or say it can't
MAX_QUESTIONS = 3
MAX_OPTIONS = 25              # Discord select-menu limit

TOOL_RULES = (
    "COMMAND RULES (follow strictly):\n"
    "- Do exactly what was asked, nothing extra. Never invent names, IDs or amounts the person did not give.\n"
    "- One message can hold several requests: call one function per request (max 10), in the order asked, each with its "
    "own arguments. Never merge different requests into one call and never repeat the same call.\n"
    "- If a needed detail is missing or could mean different things (several matches, unclear amount or target), call "
    "ask_user_choice with 2-6 clear options instead of guessing.\n"
    "- Only call functions that are in the tool list. If none fits, say so in plain text."
)


def choice_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": CHOICE_TOOL_NAME,
            "description": "Ask the person to pick from options when something is unclear. Shows a panel; their pick comes "
                           "back as their next message. Does not run any command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short heading, e.g. 'Which channel?'"},
                    "intro": {"type": "string", "description": "One or two sentences of context (optional)."},
                    "questions": {
                        "type": "array",
                        "description": "1-3 questions shown together.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string"},
                                "multiple": {"type": "boolean", "description": "true if several options may be picked"},
                                "options": {
                                    "type": "array",
                                    "description": "2-25 options",
                                    "items": {"type": "object", "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                        "value": {"type": "string", "description": "exact id/name to use if picked"},
                                    }, "required": ["label"]},
                                },
                            },
                            "required": ["prompt", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    }


def _s(value: Any, n: int) -> str:
    return (value if isinstance(value, str) else ("" if value is None else str(value))).strip()[:n]


def sanitize(args: Any) -> Optional[Dict[str, Any]]:
    """The model's raw arguments -> a safe, bounded panel spec, or None when there is nothing usable to show."""
    if not isinstance(args, dict):
        return None
    questions = []
    for qi, q in enumerate((args.get("questions") or [])[:MAX_QUESTIONS]):
        if not isinstance(q, dict):
            continue
        options, seen = [], set()
        for o in (q.get("options") or [])[:MAX_OPTIONS]:
            if isinstance(o, str):
                o = {"label": o}
            if not isinstance(o, dict):
                continue
            label = _s(o.get("label"), 80)
            value = _s(o.get("value"), 100) or label
            if not label or value in seen:
                continue
            seen.add(value)
            options.append({"label": label, "description": _s(o.get("description"), 100), "value": value})
        prompt = _s(q.get("prompt"), 200)
        if prompt and len(options) >= 2:
            questions.append({"id": f"q{qi}", "prompt": prompt, "multiple": bool(q.get("multiple")), "options": options})
    if not questions:
        return None
    return {"title": _s(args.get("title"), 100) or "Quick question", "intro": _s(args.get("intro"), 600),
            "questions": questions}


def picked_labels(spec: dict, answers: Dict[str, List[int]]) -> List[str]:
    out = []
    for q in spec["questions"]:
        idx = answers.get(q["id"]) or []
        out.append(f"{q['prompt']} -> " + ", ".join(q["options"][i]["label"] for i in idx if 0 <= i < len(q["options"])))
    return out


def followup_message(spec: dict, answers: Dict[str, List[int]], original: str = "") -> str:
    """What the AI is told after the person answers. Carries the original request so it can carry on where it stopped."""
    lines = []
    for q in spec["questions"]:
        chosen = [q["options"][i] for i in (answers.get(q["id"]) or []) if 0 <= i < len(q["options"])]
        lines.append(f"- {q['prompt']} -> " + ", ".join(f"{o['label']} (value: {o['value']})" for o in chosen))
    head = f'(I answered your question "{spec["title"]}")\n' + "\n".join(lines)
    if original:
        head += f'\nMy original request was: "{original[:600]}". Please carry it out now with these answers.'
    return head[:1500]
