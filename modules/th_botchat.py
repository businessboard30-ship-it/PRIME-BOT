# path: modules/th_botchat.py

"""
Bot-to-bot chat test (harness) -- pure logic, no Discord calls.

One test bot mentions another bot, the other answers with its normal AI chat, and (when both are harness clones) they keep
talking until a hard cap. Safety rules, all enforced from here:

  * HARD CAP on the chain: `BOT_CHAIN_MAX` consecutive bot-authored messages in one reply chain, counted from Discord's own reply
    references (no shared state between the two bot processes). The next bot sees the depth and stays silent. So it can never loop.
  * Only harness clones in the test server answer a bot (`peer_ok`), and never themselves.
  * A bot-authored turn never gets command tools, quick-answer writes, attachments or voice (enforced in cogs/ai_tools.py).
"""

from typing import Iterable, List, Optional, Sequence

BOT_CHAIN_MAX = 4              # opener + 3 answers; the 4th message is the last one anybody answers
WAIT_FIRST = 60.0              # seconds to wait for the other bot's first answer
WAIT_NEXT = 45.0               # seconds of silence that ends the watch
OPENERS = (
    "hey! quick test chat: what's one thing you can help me with?",
    "hi there, testing bot-to-bot chat. how are you doing today?",
    "hello! this is a chat test. tell me something interesting in one sentence.",
)
ERROR_MARKERS = ("ai service error", "try again later", "daily limit", "limit reached", "i can't talk to other bots")


def opener(i: int = 0) -> str:
    return OPENERS[i % len(OPENERS)]


def chain_depth(is_bot_flags: Iterable[bool]) -> int:
    """Consecutive bot-authored messages, starting with the INCOMING message then its reply ancestors (nearest first)."""
    n = 0
    for f in is_bot_flags:
        if not f:
            break
        n += 1
    return n


def may_answer(depth: int, cap: int = BOT_CHAIN_MAX) -> bool:
    return depth < cap


def peer_ok(*, in_test_guild: bool, this_bot_is_harness: bool, author_id: int, my_id: int, peer_ids: Iterable[int]) -> bool:
    """May this bot answer a message written by another BOT? Only a harness clone, in the test server, to a known harness peer."""
    return bool(in_test_guild and this_bot_is_harness and author_id != my_id and author_id in set(peer_ids))


def looks_like_error(text: Optional[str]) -> bool:
    t = (text or "").strip().lower()
    return (not t) or any(m in t for m in ERROR_MARKERS)


def verdict(*, expect_chain: bool, replies: Sequence[dict], total_seen: int) -> dict:
    """replies: [{"who": "them"|"me", "secs": float, "text": str}] in order, NOT including the opener.
    expect_chain (the other bot is a harness peer): all BOT_CHAIN_MAX messages should appear. Otherwise one answer is enough."""
    theirs = [r for r in replies if r["who"] == "them"]
    first = theirs[0] if theirs else None
    checks = [
        {"label": "the other bot answered" + (f" in {first['secs']:.1f}s" if first else ""), "ok": first is not None},
        {"label": "its answer was a real reply, not an error", "ok": first is not None and not looks_like_error(first["text"])},
    ]
    if expect_chain:
        checks.append({"label": f"the chat ran to the cap ({total_seen}/{BOT_CHAIN_MAX} messages)", "ok": total_seen == BOT_CHAIN_MAX})
        checks.append({"label": "it stopped at the cap (nothing past it)", "ok": total_seen <= BOT_CHAIN_MAX})
    return {"pass": all(c["ok"] for c in checks), "checks": checks, "gaps": [c["label"] for c in checks if not c["ok"]]}


def format_report(target_name: str, result: dict, expect_chain: bool) -> str:
    lines = [f"🤖 **Bot chat with {target_name}: {'PASS' if result['pass'] else 'FAIL'}**"
             + ("" if expect_chain else " (not one of my test clones, so I only checked that it answers)")]
    for c in result["checks"]:
        lines.append(f"  {'✓' if c['ok'] else '✗'} {c['label']}")
    lines.append("**Gaps:** " + ("; ".join(result["gaps"]) if result["gaps"] else "none found"))
    return "\n".join(lines)[:1990]
