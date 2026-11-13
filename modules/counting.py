# path: modules/counting.py

"""Counting game rules. Pure: no Discord, no database, so every rule is unit-testable.

Members count up together in one channel, one number each, never the same person twice in a row.
A wrong number (or counting twice in a row) ruins the run and it starts again from 1.

Free: the channel game, reactions, best score, stats and a top-5 board.
Premium (applied only while the server has Premium; settings are kept if it lapses):
  * maths   : "2*3+1" counts as 7
  * saves   : a mistake spends a save instead of ruining the run (1 to start, +1 every SAVE_EVERY counts, max SAVES_MAX)
  * milestones: a shout-out card every N counts
  * a top-15 board
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

MAX_DIGITS = 12                 # plain numbers: 1 to 12 digits
MAX_EXPR_LEN = 40               # maths messages longer than this are just chat
MAX_VALUE = 10 ** 18            # no intermediate result may exceed this (keeps the evaluator cheap)
SAVES_MAX = 3
SAVES_START = 1
SAVE_EVERY = 50                 # Premium: +1 save per this many correct counts
MILESTONE_MIN, MILESTONE_MAX = 10, 10_000
FREE_TOP, PREMIUM_TOP = 5, 15

OK, WRONG, SAME_USER = "ok", "wrong", "same_user"

FREE_PERKS = "Counting channel, ✅/❌ reactions, best score, stats and a top 5 board."
PREMIUM_PERKS = (f"Maths counting (`2*3+1`), saves (a mistake doesn't reset the run: {SAVES_START} to start, "
                 f"+1 every {SAVE_EVERY} counts, max {SAVES_MAX}), milestone shout-outs and a top {PREMIUM_TOP} board.")

_MATH_CHARS = re.compile(r"[0-9+\-*/() ]+")
_PLAIN = re.compile(rf"\d{{1,{MAX_DIGITS}}}")


# ── parsing ─────────────────────────────────────────────────────────────

def _eval_node(node: ast.AST, depth: int = 0) -> Optional[int]:
    """Whole-number maths only: + - * / (exact division) and unary minus. Anything else, or any result above
    MAX_VALUE, gives None. Never raises."""
    if depth > 12:
        return None
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value if abs(node.value) <= MAX_VALUE else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval_node(node.operand, depth + 1)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        a, b = _eval_node(node.left, depth + 1), _eval_node(node.right, depth + 1)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            r = a + b
        elif isinstance(node.op, ast.Sub):
            r = a - b
        elif isinstance(node.op, ast.Mult):
            r = a * b
        else:
            if b == 0 or a % b != 0:
                return None
            r = a // b
        return r if abs(r) <= MAX_VALUE else None
    return None


def eval_math(text: str) -> Optional[int]:
    """'2*3+1' -> 7. Needs at least one binary operator (so '-5' in chat is not an attempt). None when it isn't clean maths."""
    t = (text or "").strip()
    if not t or len(t) > MAX_EXPR_LEN or not _MATH_CHARS.fullmatch(t) or not any(c.isdigit() for c in t):
        return None
    try:
        tree = ast.parse(t, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    if not any(isinstance(n, ast.BinOp) for n in ast.walk(tree)):
        return None
    try:
        return _eval_node(tree.body)
    except (RecursionError, OverflowError, ValueError):
        return None


def parse_number(text: Optional[str], allow_math: bool = False) -> Optional[int]:
    """The number a message is trying to count, or None when it is just chat (chat is ignored, never punished)."""
    t = (text or "").strip()
    if not t:
        return None
    if _PLAIN.fullmatch(t):
        return int(t)
    return eval_math(t) if allow_math else None


def valid_milestone(raw: Optional[str]) -> Optional[int]:
    """0 (off) or MILESTONE_MIN..MILESTONE_MAX from what an admin typed; None when it isn't valid."""
    try:
        n = int(str(raw or "").strip())
    except ValueError:
        return None
    return n if n == 0 or MILESTONE_MIN <= n <= MILESTONE_MAX else None


# ── the rules ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Outcome:
    kind: str                    # 'ok' | 'ruined' | 'saved' | 'hint' (a wrong first number: nothing to lose yet)
    number: int = 0              # the count after this message
    reason: str = ""             # 'wrong' | 'same_user' (for ruined / saved / hint)
    lost: int = 0                # the run that was lost (ruined)
    saves_left: int = 0
    new_record: bool = False
    earned_save: bool = False
    milestone: bool = False


def judge(current: int, last_user_id: Optional[int], user_id: int, number: int) -> str:
    if last_user_id is not None and last_user_id == user_id:
        return SAME_USER
    return OK if number == current + 1 else WRONG


def step(state: Dict, user_id: int, number: int, *, saves_on: bool = False,
         milestone_every: int = 0) -> Tuple[Dict, Outcome]:
    """One attempt. `state` holds current, high_score, last_user_id, saves, total_correct, total_ruined.
    Returns (new state with only those keys, outcome). Pure: the caller stores the new state."""
    cur = int(state.get("current") or 0)
    high = int(state.get("high_score") or 0)
    last = state.get("last_user_id")
    saves = int(state.get("saves") if state.get("saves") is not None else SAVES_START)
    correct = int(state.get("total_correct") or 0)
    wrong = int(state.get("total_ruined") or 0)

    verdict = judge(cur, last, user_id, number)
    if verdict == OK:
        cur, correct = cur + 1, correct + 1
        record = cur > high
        high = max(high, cur)
        earned = bool(saves_on and cur % SAVE_EVERY == 0 and saves < SAVES_MAX)
        saves += 1 if earned else 0
        ms = bool(milestone_every and cur % milestone_every == 0)
        new = {"current": cur, "high_score": high, "last_user_id": user_id, "saves": saves,
               "total_correct": correct, "total_ruined": wrong}
        return new, Outcome("ok", cur, new_record=record, earned_save=earned, milestone=ms, saves_left=saves)

    if cur == 0:                                   # nothing to lose before the first count
        return dict(state), Outcome("hint", 0, reason=verdict)
    wrong += 1
    if saves_on and saves > 0:
        saves -= 1
        new = {"current": cur, "high_score": high, "last_user_id": last, "saves": saves,
               "total_correct": correct, "total_ruined": wrong}
        return new, Outcome("saved", cur, reason=verdict, saves_left=saves)
    new = {"current": 0, "high_score": high, "last_user_id": None, "saves": saves,
           "total_correct": correct, "total_ruined": wrong}
    return new, Outcome("ruined", 0, reason=verdict, lost=cur, saves_left=saves)


# ── text (pure) ─────────────────────────────────────────────────────────

def _why(reason: str, current: int) -> str:
    return "You can't count twice in a row." if reason == SAME_USER else f"The next number was **{current + 1}**."


def outcome_text(name: str, out: Outcome, current_before: int, high: int) -> Optional[str]:
    """The channel message for an outcome; None when a reaction is enough."""
    if out.kind == "ruined":
        best = f" Best: **{high}**." if high else ""
        return f"💥 **{name}** ruined it at **{out.lost}**! {_why(out.reason, current_before)}{best} Start again from **1**."
    if out.kind == "saved":
        return (f"🛟 **{name}** slipped up, but a save kept the run alive at **{out.number}**. "
                f"{_why(out.reason, current_before)} Saves left: **{out.saves_left}**.")
    if out.kind == "hint":
        return f"Counting starts at **1**. {'Take turns, ' + name + '!' if out.reason == SAME_USER else ''}".strip()
    return None


def stats_text(cfg: Dict, rows, premium: bool) -> str:
    """The stats card. `rows` are (user_id, correct, wrong) best first."""
    cur, high = int(cfg.get("current") or 0), int(cfg.get("high_score") or 0)
    lines = ["## 🔢 Counting", f"**Current:** {cur} · **Next:** {cur + 1} · **Best:** {high}",
             f"**Counted:** {int(cfg.get('total_correct') or 0)} · **Mistakes:** {int(cfg.get('total_ruined') or 0)}"]
    if premium and cfg.get("saves_on"):
        lines.append(f"**Saves:** {int(cfg.get('saves') if cfg.get('saves') is not None else SAVES_START)}/{SAVES_MAX}")
    lines += ["", "**Top counters**"]
    shown = list(rows)[: (PREMIUM_TOP if premium else FREE_TOP)]
    if shown:
        for i, (uid, ok, bad) in enumerate(shown, 1):
            total = ok + bad
            acc = f" · {round(100 * ok / total)}%" if total else ""
            lines.append(f"{i}. <@{uid}> — {ok}{acc}")
    else:
        lines.append("Nobody has counted yet. Start with **1**!")
    if not premium:
        lines.append(f"-# 💎 Premium shows a top {PREMIUM_TOP}.")
    return "\n".join(lines)
