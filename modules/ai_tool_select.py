"""Pick the few command tools worth sending with one AI chat message.

Why: sending every command as a tool (100+ of them, ~10k tokens) blew Groq's 8,000 tokens-per-minute cap on its own
(HTTP 413), which a backup key can't fix, so every /aichat turn ended in "AI service error". Only the commands that
look relevant to what the person typed are sent; a message that matches nothing goes out as plain chat (no tools).
"""
import json
import re
from typing import Dict, List

MAX_TOOLS = 12
MAX_TOOL_TOKENS = 3000        # rough budget for the whole tool list (len(json)/4)
PER_CLAUSE_TOOLS = 3          # tools kept for each part of a many-demands sentence
MAX_TOOLS_MULTI = 16          # all parts together
MAX_TOOL_TOKENS_MULTI = 4200  # a little more room when one sentence holds several demands

_STOP = frozenset("""the and for you your can could would please with that this what when where how why who are was were
has have had not but all any get got let lets make just like want need then them they its out off now run use using
show tell give about into from some more less bot command commands maxwell hey hello thanks thank""".split())


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP]


def _matches(word: str, token: str) -> bool:
    if word == token:
        return True
    short, long_ = sorted((word, token), key=len)
    if len(short) >= 3 and long_.startswith(short) and len(long_) - len(short) <= 4:
        return True                                  # ban/banned/banning, level/levels
    n = min(len(word), len(token), 5)
    return n >= 4 and word[:n] == token[:n]          # crude stemming: level/levels/leveling, ban/banned


def _score(words: List[str], tool: Dict) -> int:
    fn = tool.get("function", {})
    name = _words((fn.get("name") or "").replace("_", " "))
    desc = _words(fn.get("description") or "")
    params = _words(" ".join((fn.get("parameters", {}).get("properties") or {}).keys()))
    score = 0
    for w in set(words):
        if any(_matches(w, t) for t in name):
            score += 3
        if any(_matches(w, t) for t in desc):
            score += 1
        if any(_matches(w, t) for t in params):
            score += 1
    return score


def select_tools(tools: List[Dict], message: str, limit: int = MAX_TOOLS) -> List[Dict]:
    """Top `limit` tools by relevance to `message`, best first, within a token budget. [] when nothing matches."""
    if not tools:
        return []
    words = _words(message)
    if not words:
        return []
    ranked = sorted(((_score(words, t), i, t) for i, t in enumerate(tools)), key=lambda r: (-r[0], r[1]))
    chosen, used = [], 0
    for score, _i, tool in ranked:
        if score <= 0 or len(chosen) >= limit:
            break
        cost = len(json.dumps(tool)) // 4
        if chosen and used + cost > MAX_TOOL_TOKENS:
            continue
        chosen.append(tool)
        used += cost
    return chosen


_CLAUSE_SPLIT = re.compile(r"[.;!?\n]+|,|\b(?:and then|after that|and also|and|then|also|plus|next)\b", re.I)


def split_clauses(message: str) -> List[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(message or "") if c and _words(c)]


def select_tools_multi(tools: List[Dict], message: str) -> List[Dict]:
    """Like select_tools, but for a message holding several demands: each part of the sentence gets its own best
    tools, so the 10th demand is not crowded out by the first three. One-part messages behave exactly as before."""
    clauses = split_clauses(message)
    if len(clauses) < 2:
        return select_tools(tools, message)
    chosen, seen, used = [], set(), 0
    for clause in clauses:
        for tool in select_tools(tools, clause, limit=PER_CLAUSE_TOOLS):
            name = tool.get("function", {}).get("name")
            if name in seen:
                continue
            cost = len(json.dumps(tool)) // 4
            if len(chosen) >= MAX_TOOLS_MULTI or (chosen and used + cost > MAX_TOOL_TOKENS_MULTI):
                continue
            seen.add(name)
            chosen.append(tool)
            used += cost
    return chosen or select_tools(tools, message)
