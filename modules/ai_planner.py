# path: modules/ai_planner.py

"""
Phase 1 of the 20+ task upgrade: a planner in front of the AI command runner.

Why: asking the model for 20 tool calls in ONE reply cannot work (the chat call is capped at ~1500 completion tokens, only
~16 tools fit in the request, and one confused call wrecks the lot). So a big request is handled in three steps:

  1. DECOMPOSE  one model call, no tools, returns a numbered task list as JSON  -> parse_tasks() cleans and bounds it;
  2. RESOLVE    the tasks are cut into small chunks; each chunk gets its OWN short tool list and its own model call, so a
                chunk is as easy as the 3-task requests that already work (the cog does this, see ai_tools._plan_and_resolve);
  3. RUN        every resolved call goes down the normal guarded path (allowlist, real permission, role hierarchy, daily
                cap, ONE confirm, ai_plan runner). Nothing in here can run a command.

Everything here is pure (no Discord, no database) except `decompose`, which does the single Groq call and imports lazily.
Task text is UNTRUSTED model output: it is only ever used as a prompt for the next model call, never executed.
"""

import json
import re
from typing import Dict, List, Optional, Tuple

from modules.ai_tool_select import split_clauses

MAX_TASKS = 25                  # keep equal to ai_plan.MAX_STEPS (a test checks)
MIN_DEMANDS_FOR_PLANNER = 6     # fewer parts than this keep using the normal single-reply path
CHUNK_SIZE = 4                  # tasks resolved per model call
CHUNK_TOOLS = 8                 # tools offered for one chunk
CHUNK_PAUSE_SECONDS = 3.0       # between chunk calls (Groq's tokens-per-minute cap)
TASK_PAUSE_SECONDS = 1.5        # between single-task calls when a chunk has to be redone one task at a time
MAX_PLANNING_SECONDS = 240      # wall clock for the whole decompose+resolve phase
MAX_TASK_CHARS = 300
MAX_MESSAGE_CHARS = 2000

SYSTEM_PROMPT = (
    "You split a Discord server admin's request into separate tasks. Output ONLY JSON, no prose, no code fences:\n"
    '{"tasks":[{"id":1,"task":"<one concrete action>","depends_on":[]}]}\n'
    "Rules: one action per task (create ONE category, create the channels inside it as ONE task, lock ONE channel). "
    "Keep the person's names, numbers and spelling exactly. Keep the order they asked for. "
    "depends_on lists ids of EARLIER tasks that must succeed first (e.g. channels inside a category made by task 1); "
    "otherwise []. Do not invent tasks, do not merge unrelated actions, do not add safety or confirmation tasks."
)


# Real separate demands: sentences, lines, numbered items and "then/also/plus/next" -- NOT bare commas or a plain "and",
# which usually just separate the names in ONE bulk request ("create channels a, b, c and d").
_STRICT_SPLIT = re.compile(r"[.;!?\n]+|(?:^|\s)\d{1,2}[.)]\s|\b(?:and then|after that|and also|then|also|plus|next|finally|afterwards)\b", re.I)
STRICT_DEMANDS_FOR_PLANNER = 4


def count_demands(message: str) -> int:
    return len([c for c in _STRICT_SPLIT.split(message or "") if c and re.search(r"[a-z]{3,}", c, re.I)])


def should_plan(message: str) -> bool:
    """True for a request with enough separate parts to be worth planning (the normal path handles the rest): 4+ clearly
    separate demands, or 10+ parts even when only commas separate them. A list of names for one bulk command stays on
    the normal path."""
    msg = message or ""
    return count_demands(msg) >= STRICT_DEMANDS_FOR_PLANNER or len(split_clauses(msg)) >= MIN_DEMANDS_FOR_PLANNER + 4


def decompose_messages(message: str) -> List[Dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (message or "")[:MAX_MESSAGE_CHARS]}]


def _json_blob(raw: str) -> Optional[object]:
    """The OUTERMOST JSON value in the text (a bare list of objects must come back as the list, not its first object)."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    spans = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            spans.append((start, end))
    for start, end in sorted(spans):                      # earliest opener first = outermost
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            continue
    return None


def parse_tasks(raw: str, limit: int = MAX_TASKS) -> Tuple[List[Dict], Dict]:
    """Model text -> ([{'id','task','depends_on'}], info).  ids are renumbered 1..n in the order given; depends_on only
    keeps ids of EARLIER tasks (a forward or unknown reference can never be satisfied, so it is dropped, which also rules
    out cycles); blank and repeated tasks are removed; at most `limit` tasks survive.
    info = {'dropped_over_limit': int, 'duplicates': int, 'bad': int}."""
    info = {"dropped_over_limit": 0, "duplicates": 0, "bad": 0}
    data = _json_blob(raw)
    items = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], info
    tasks, seen, remap = [], set(), {}
    for pos, it in enumerate(items, 1):
        if isinstance(it, str):
            it = {"task": it}
        if not isinstance(it, dict):
            info["bad"] += 1
            continue
        text = " ".join(str(it.get("task") or it.get("text") or "").split())[:MAX_TASK_CHARS]
        if not text:
            info["bad"] += 1
            continue
        key = text.lower()
        if key in seen:
            info["duplicates"] += 1
            continue
        if len(tasks) >= limit:
            info["dropped_over_limit"] += 1
            continue
        seen.add(key)
        new_id = len(tasks) + 1
        old_id = it.get("id", pos)
        remap[old_id if isinstance(old_id, int) else pos] = new_id
        deps = []
        for d in (it.get("depends_on") or []):
            if isinstance(d, int) and remap.get(d) is not None and remap[d] < new_id and remap[d] not in deps:
                deps.append(remap[d])
        tasks.append({"id": new_id, "task": text, "depends_on": deps})
    return tasks, info


def chunk_tasks(tasks: List[Dict], size: int = CHUNK_SIZE) -> List[List[Dict]]:
    """Consecutive groups of at most `size` tasks, order kept. A task stays in the same or a LATER chunk than the tasks it
    depends on, so a chunk never needs something that has not been resolved yet."""
    size = max(1, size)
    return [tasks[i:i + size] for i in range(0, len(tasks), size)]


def chunk_prompt(original: str, chunk: List[Dict], earlier: List[Dict]) -> str:
    """The request for ONE chunk's model call. `earlier` are tasks already resolved (named so the model doesn't redo them)."""
    lines = "\n".join(f"{t['id']}. {t['task']}" for t in chunk)
    done = ("Already handled by earlier steps (do NOT call tools for these): "
            + "; ".join(f"{t['id']}. {t['task']}" for t in earlier[-6:]) + "\n") if earlier else ""
    return (f"Original request: {(original or '')[:MAX_MESSAGE_CHARS]}\n\n{done}"
            f"Now call the right tool for EACH of these tasks, in this order, all in this one reply:\n{lines}\n"
            "One tool call per task. If a task has no matching tool, skip it. Don't write a plan or ask a question; just make the calls.")


def unresolved(tasks: List[Dict], calls_per_chunk: List[Tuple[List[Dict], int]]) -> List[Dict]:
    """Tasks of chunks whose model call gave no tool calls at all (so the person can be told exactly what was not planned).
    calls_per_chunk: [(chunk_tasks, number_of_calls_returned)]."""
    out = []
    for chunk, n in calls_per_chunk:
        if n == 0:
            out.extend(chunk)
    return out


async def decompose(message: str) -> Tuple[List[Dict], Dict]:
    """The single no-tools model call. ([], info) when the model is unreachable or answered something unusable, so the
    caller falls back to the normal path."""
    from modules import ai_features as af
    payload = {"model": af.AI_CHAT_MODEL, "messages": decompose_messages(message), "temperature": 0.1,
               "max_completion_tokens": 1800, "reasoning_effort": "low", "top_p": 1.0}
    status, data, _err = await af._groq_post(payload, timeout_seconds=45)
    if status != 200 or not data:
        return [], {"dropped_over_limit": 0, "duplicates": 0, "bad": 0}
    try:
        raw = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raw = ""
    return parse_tasks(raw)
