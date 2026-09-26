"""
AI Features Module — Conversational AI Chat & Image Generation
Uses Groq API for chat (anime questions, general chat)
Uses Fal AI or Replicate for image generation
Gated behind premium tier system (superbot_adapter.get_user_tier)
"""

import aiohttp
import logging
import re
from typing import Optional, List, Dict
from datetime import datetime, timezone
from database import get_pool

logger = logging.getLogger(__name__)

# AI_GATEWAY_API_KEY or provider keys from env
import os
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
FAL_API_KEY = os.getenv("FAL_API_KEY", "")
# GEMINI_API_KEY: free Google AI Studio key (aistudio.google.com/apikey) — no
# credit card, no billing setup. gemini-2.5-flash-image ("Nano Banana") has a
# genuinely generous free tier (Google's own docs: up to 500 requests/day).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ═══════════════════════════════════════════════════════════════════════════
# AI CHAT with conversation history
# ═══════════════════════════════════════════════════════════════════════════

# The model writes this token instead of a URL; render_support_link() strips it to
# plain wording and leaves SUPPORT_BUTTON_MARKER behind so the caller attaches a real
# Discord link button — the same guaranteed-no-preview mechanism as the bot's own
# invite link, instead of trusting the model to never leak a raw/unmasked URL.
SUPPORT_TOKEN = "[[SUPPORT]]"
SUPPORT_BUTTON_MARKER = "\x00SUPPORT_BTN\x00"
BOT_NAME = "Maxwell"  # what the AI says when asked its name

AI_CHAT_MODEL = "openai/gpt-oss-120b"  # Groq's current recommended general-purpose model
# Shared rules appended to every prompt. Length is enforced three ways —
# this prompt, a low max_completion_tokens, and trim_reply() — because a
# prompt-only length limit isn't reliable.
BOT_RULES = (
    "You are the assistant of THIS Discord bot and its branded clones, chatting inside Discord. Rules:\n"
    "1. Answer in 1-3 short sentences, under 300 characters. Plain text, no headings, no bullet lists "
    "(exception: a how-to may use up to 4 very short lines). Never ramble.\n"
    "2. Only talk about this bot, its commands and features, or light general/anime chat. Never recommend, "
    "compare, explain or mention other Discord bots (MEE6, Dyno, Carl-bot, etc.). If asked about another "
    "bot, say you only help with this bot.\n"
    "3. Never invent commands or features. Only use commands you were explicitly given.\n"
    "4. AI chat has no paid credits or top-ups. If asked about buying AI credits, say that isn't a thing. "
    "Premium is a per-server subscription that raises the daily AI chat limit; it is not AI credits.\n"
    f"5. If a question about this bot is too hard, too detailed, or you are not sure of the answer, do NOT "
    f"guess. Say you're not sure and send them to the support server by writing {SUPPORT_TOKEN} at the end, "
    f"for example: \"Not sure about that one, the support server can help, tap {SUPPORT_TOKEN}\". "
    f"Never write a URL or invite link yourself, only {SUPPORT_TOKEN}.\n"
    "6. If the question is about the specific server they are in (its rules, roles, channels, staff, bans, "
    "events, or anything only that server controls), say you can't answer that and tell them to talk to a "
    "server admin or contact that server's support/staff. Do not send those to the bot's support server.\n"
    f"7. Your name is {BOT_NAME}. If someone asks your name or who you are, simply say you're {BOT_NAME}. Keep it short.\n"
    "8. The person is already chatting with you. Never tell them to use /aichat or /ai chat to talk to you; just answer.\n"
    "9. /levelrole giftboost exists but is bot-owner only — never suggest it to anyone as a way to get an XP "
    "boost. If asked how to boost XP, only mention the Boost XP button.\n"
    "10. Clans: every member is auto-locked to one of 5 random clans, shown on a flavor card every 3 levels. "
    "5 clan-chief seats exist per server, held by whoever is rank #1-5 on that server's XP leaderboard — "
    "overtaking a chief takes their exact seat and title, even if it's a different clan than your own. Chief "
    "status shows on /rank and /leaderboard. Chiefs get their clan card every level-up instead of every 3."
)
SYSTEM_PROMPT_ANIME = (
    "You are an anime expert. Be friendly and conversational about anime, manga, characters and recommendations.\n"
    + BOT_RULES
)
SYSTEM_PROMPT_GENERAL = (
    "You are a helpful, friendly assistant.\n" + BOT_RULES
)

OTHER_BOT_REFUSAL = "I can only help with this bot and its features — try /help to see what I can do."

# Names that come up in practice. Deliberately excludes bot names that are
# also ordinary words (Arcane, Wick, Groovy) to avoid false positives.
_OTHER_BOTS = re.compile(
    r"\b(mee6|dyno(?:bot)?|carl[\s-]?bot|probot|dank\s?memer|mudae|ticket\s?tool|yagpdb|statbot|fredboat|"
    r"rythm|tatsu(?:maki)?|unbelievaboat|poke\s?two|pok[eé]two|midjourney\s+bot|giveawaybot|vexera|"
    r"sesh\s?bot|mimu|mee\s?six)\b",
    re.IGNORECASE,
)


def mentions_other_bot(text: str) -> bool:
    return bool(text and _OTHER_BOTS.search(text))


def trim_reply(text: str, limit: int = 600) -> str:
    """Hard backstop for brevity: collapse blank runs and cut at the last
    sentence end (or word) under `limit`."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if m >= limit // 2:
        return cut[: m + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


_SUPPORT_TOKEN_RE = re.compile(r"\[\[\s*SUPPORT\s*\]\]", re.IGNORECASE)


def render_support_link(text: str) -> str:
    """Strip the [[SUPPORT]] token to plain wording ("our support server") and,
    if a support invite is configured, append SUPPORT_BUTTON_MARKER so the caller
    (ai_tools.py) attaches an actual discord.ui.Button link — never a URL in the
    message text itself. This makes the support link behave exactly like the bot's
    own invite link: a real button, masked by construction, not by hoping the model
    always uses the token instead of writing the URL out itself."""
    if not text:
        return text
    if not _SUPPORT_TOKEN_RE.search(text):
        return text
    text = _SUPPORT_TOKEN_RE.sub("our support server", text).rstrip()
    try:
        from config import DISCORD_SUPPORT_SERVER_INVITE as invite
    except Exception:
        invite = ""
    if invite:
        text = f"{text} {SUPPORT_BUTTON_MARKER}"
    return text


# Premium / credits questions get a fixed answer + the Go Premium button
# (not left to the model), so the wording is always exact.
_PREMIUM_Q = re.compile(
    r"\b(premium|subscri(?:be|ption)|upgrade|top[\s-]?ups?|"
    r"(?:buy|get|purchase|need|more|ai|chat)\s+credits?|"
    r"credits?\s+(?:for|to)\s+(?:the\s+)?(?:ai|chat|bot))\b",
    re.IGNORECASE,
)


def is_premium_question(text: str) -> bool:
    return bool(text and _PREMIUM_Q.search(text))


# "How do I add/invite the bot to my server?" -> the caller builds the bot's own
# OAuth invite link (correct for the main bot and every clone).
_BOT_INVITE_Q = re.compile(
    r"\b(?:invite|add|get|put|bring)\s+(?:you|u|yourself|the\s+bot|this\s+bot|your\s+bot|the\s+\w+\s+bot)\b"
    r"|\b(?:bot|your|the\s+bot'?s)\s+(?:invite|invitation)\b"
    r"|\binvit(?:e|ation)\s+(?:link|url)\s+(?:for|of|to)\s+(?:the\s+|this\s+|your\s+)?bot\b"
    r"|\bhow\s+(?:do|can|could|to)\s+(?:i\s+|we\s+)?(?:invite|add)\s+(?:it|this)\s+(?:bot\s+)?to\b",
    re.IGNORECASE,
)
# "How do I join your support server / link to the support server?"
_SUPPORT_INVITE_Q = re.compile(
    r"\bsupport\s+(?:server|group|discord|guild)\b.{0,30}\b(?:link|invite|join)\b"
    r"|\b(?:link|invite|join)\b.{0,30}\bsupport\s+(?:server|group|discord|guild)\b",
    re.IGNORECASE,
)


def is_support_invite_question(text: str) -> bool:
    return bool(text and _SUPPORT_INVITE_Q.search(text))


def is_bot_invite_question(text: str) -> bool:
    # A support-server ask is handled separately, so it never gets the bot's invite.
    return bool(text and not is_support_invite_question(text) and _BOT_INVITE_Q.search(text))


# Safety net: if the model ignores rule 5 and writes/paraphrases a raw invite URL
# instead of the [[SUPPORT]] token, catch it here too so it still comes out as a
# button instead of a bare link Discord could preview.
_RAW_SUPPORT_URL_RE = re.compile(r"https?://(?:www\.)?discord\.gg/\S+", re.IGNORECASE)


def scrub_raw_support_url(text: str) -> str:
    if not text or not _RAW_SUPPORT_URL_RE.search(text):
        return text
    try:
        from config import DISCORD_SUPPORT_SERVER_INVITE as invite
    except Exception:
        invite = ""
    text = _RAW_SUPPORT_URL_RE.sub("our support server", text).rstrip()
    if invite and SUPPORT_BUTTON_MARKER not in text:
        text = f"{text} {SUPPORT_BUTTON_MARKER}"
    return text


# Safety net for BOT_RULES #9: if the model ignores the rule and still names
# the owner-only /levelrole giftboost subcommand, strip that mention out and
# redirect to the actual user-facing option (Boost XP).
_GIFTBOOST_MENTION_RE = re.compile(
    r"/?levelrole\s+giftboost\b|\blevelrole\s+gift\s*boost\b|\bgift\s*boost\s+command\b",
    re.IGNORECASE,
)


def scrub_giftboost_mention(text: str) -> str:
    if not text or not _GIFTBOOST_MENTION_RE.search(text):
        return text
    return _GIFTBOOST_MENTION_RE.sub("the Boost XP button", text).rstrip()


def support_invite_answer() -> str:
    return render_support_link("Here you go, tap [[SUPPORT]] to join the support server.")


# "How do I level up / get XP faster?" -> short explanation + Boost XP button
# (not left to the model, same reasoning as premium/credits above).
_LEVELUP_Q = re.compile(
    r"\blevel(?:ing)?\s*up\b|\bhow\s+(?:do|can|could)\s+i\s+level\s*up\b|"
    r"\b(?:get|gain|earn|need)\s+(?:more\s+)?xp\b|\bxp\s+faster\b|\bboost\s+(?:my\s+)?xp\b",
    re.IGNORECASE,
)


def is_levelup_question(text: str) -> bool:
    return bool(text and _LEVELUP_Q.search(text))


def levelup_answer(in_server: bool) -> str:
    """Fixed reply to level-up/XP questions. In a server it goes with the
    Boost XP button (added by the caller)."""
    if not in_server:
        return (
            "📈 You level up by chatting — XP comes from sending messages, with a short cooldown "
            "between each. Ask me this inside a server to see the Boost XP option."
        )
    return (
        "📈 You gain XP just by chatting (with a short cooldown between messages) — the more active "
        "you are, the faster you level up. Want it quicker? Tap **Boost XP** below for a temporary "
        "XP multiplier."
    )


def premium_answer(in_server: bool) -> str:
    """Fixed reply to premium/credits questions. In a server it goes with the
    Go Premium button (added by the caller)."""
    if not in_server:
        return (
            "💎 Premium is a **per-server** subscription. It is **not** AI credits, and you are not "
            "buying credits. Ask me this inside the server you want it for and I'll show you the "
            "Go Premium button."
        )
    return (
        "💎 **You are not buying credits.** Premium is a subscription that anyone can get, but it only "
        "applies to **this server**, not every server you're in. It raises this server's daily AI chat "
        f"limit from {REPLY_CAP_NORMAL} to {REPLY_CAP_PREMIUM} and unlocks every premium feature here. "
        "Tap **Go Premium** below."
    )


def until_reset_text() -> str:
    """'5h 12m' until the next midnight UTC (when daily caps reset)."""
    now = datetime.now(timezone.utc)
    nxt = (now.replace(hour=0, minute=0, second=0, microsecond=0)).replace(tzinfo=timezone.utc)
    from datetime import timedelta
    secs = int((nxt + timedelta(days=1) - now).total_seconds())
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# User AI usage caps (per tier)
AI_USAGE_CAPS = {
    "basic": {"daily_messages": 10, "daily_images": 1},
    "pro": {"daily_messages": 100, "daily_images": 10},
    "elite": {"daily_messages": 1000, "daily_images": 100},
    "founder": {"daily_messages": 10000, "daily_images": 10000},
}

# How many past turns (one turn = one user message + the bot's reply) get
# fed back to the model as context. Founder/elite get deeper memory as a
# tier perk instead of everyone sharing one flat limit.
AI_HISTORY_TURNS = {
    "basic": 3,
    "pro": 5,
    "elite": 10,
    "founder": 10,
}

# Stored prompt/response text is capped for sane row sizes and token budget,
# but this is *storage* truncation (way above one Discord message), not the
# old mid-sentence 200-char context cut.
MAX_STORED_TEXT = 4000


async def get_user_ai_usage(user_id: int, usage_type: str = "messages") -> int:
    """Get today's AI usage count for user (messages or images)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            table = "ai_chat_usage" if usage_type == "messages" else "ai_image_usage"
            # Reply/DM chat has its own per-server cap (get_reply_usage), so
            # it must not eat into the /aichat tier limit.
            extra = " AND COALESCE(kind, 'command') <> 'reply'" if usage_type == "messages" else ""
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = $1 "
                f"AND DATE(created_at) = (NOW() AT TIME ZONE 'UTC')::date{extra}",
                user_id
            )
        return count or 0
    except Exception as e:
        logger.error(f"[v0] Error getting AI usage: {e}")
        return 0


async def log_ai_usage(user_id: int, usage_type: str = "messages", prompt_text: str = "",
                        response_text: str = None, session_id: int = None,
                        guild_id: Optional[int] = None, kind: Optional[str] = None) -> bool:
    """Log AI feature usage for rate limiting. For chat messages, also
    stores the bot's response (and the session it belongs to, if any) so
    get_ai_conversation_history can replay real back-and-forth turns
    instead of just a list of past user prompts."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Both ai_chat_usage.user_id and ai_image_usage.user_id FK
                # to users(user_id) — a user who has never triggered any
                # other user-row-creating path (economy, leveling, payments,
                # etc.) won't have a row yet, and the insert below fails
                # with a foreign key violation instead of logging usage.
                # Same guard already used before payment_logs inserts.
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                    user_id,
                )
                # Postgres text columns reject NUL (0x00) bytes.
                prompt_text = (prompt_text or "").replace("\x00", "")
                response_text = response_text.replace("\x00", "") if response_text else response_text
                if usage_type == "messages":
                    await conn.execute(
                        "INSERT INTO ai_chat_usage (user_id, prompt, response, session_id, guild_id, kind, created_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, NOW())",
                        user_id, prompt_text[:MAX_STORED_TEXT],
                        response_text[:MAX_STORED_TEXT] if response_text else None,
                        session_id, guild_id, kind,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO ai_image_usage (user_id, prompt, created_at) VALUES ($1, $2, NOW())",
                        user_id, prompt_text[:500]
                    )
        return True
    except Exception as e:
        logger.error(f"[v0] Error logging AI usage: {e}")
        return False


async def get_ai_conversation_history(session_id: int, limit_turns: int = 3) -> List[Dict]:
    """Get recent conversation turns (prompt + response pairs) for a
    specific active session, oldest first — ready to replay as alternating
    user/assistant messages. Scoped to session_id rather than "last N rows
    for this user" so a /newchat cleanly starts from empty context instead
    of bleeding in an unrelated earlier conversation."""
    if not session_id:
        return []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT prompt, response, created_at FROM ai_chat_usage "
                "WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
                session_id, limit_turns
            )
            return [dict(row) for row in reversed(rows)]
    except Exception as e:
        logger.error(f"[v0] Error fetching conversation history: {e}")
        return []


async def get_or_create_active_session(user_id: int) -> int:
    """/aichat calls this so a session always exists without forcing users
    to run /newchat first — the first message of the day just quietly opens
    one, same convenience as before, but now everything after it remembers."""
    from database import db
    session = await db.get_active_ai_chat_session(user_id)
    if session:
        return session["id"]
    return await db.start_ai_chat_session(user_id)


async def ai_chat(user_id: int, message: str, is_anime_question: bool = False,
                   tier: str = "basic", session_id: int = None,
                   command_context: Optional[str] = None,
                   history_override: Optional[List[Dict]] = None,
                   guild_id: Optional[int] = None,
                   kind: Optional[str] = None,
                   tools: Optional[List[Dict]] = None) -> Optional[object]:
    """
    Send message to Groq API and get response.
    Returns response text (str) on success. On failure, returns None (caller
    shows a generic "AI service error" to the user) but ALSO stashes the
    real error string on `ai_chat.last_error` so an admin-facing surface
    (see handlers/ai_handler.py) can show the actual cause — missing key,
    bad model name, Groq outage, rate limit, etc. — instead of just "None".

    `session_id` scopes conversation history to one active conversation
    (see get_or_create_active_session / /newchat /endchat in
    discord_bot/cogs/ai_tools.py) — without it, this behaves like a single
    one-shot turn with no prior context. `tier` controls how many past
    turns are replayed (AI_HISTORY_TURNS), a founder/elite perk.

    `tools`: optional Groq/OpenAI-style tool schema list (see
    discord_bot.cogs.ai_tools._build_command_tools, built from
    modules.ai_command_guard.get_qualifying_commands). When omitted (the
    default, used by every existing caller), behavior is 100% unchanged —
    no tool_choice is sent and the return value is always str-or-None as
    before. When provided and the model chooses to call one, this returns
    a dict ``{"tool_calls": [{"name": str, "arguments": dict}, ...]}``
    instead of a string — callers that pass `tools` must check for that
    dict shape before treating the result as chat text. Nothing is logged
    to ai_chat_usage for a tool-call turn (there's no reply text to log);
    the eventual command attempt is logged separately by
    ai_command_guard.resolve_and_check/execute_ai_command.
    """
    ai_chat.last_error = None
    try:
        if not GROQ_API_KEY:
            ai_chat.last_error = "GROQ_API_KEY is not set in the environment."
            return "⚠️ AI service not configured. Admin needs to set GROQ_API_KEY."

        # Get conversation history for this specific session, both sides of
        # each turn (not just past user prompts), oldest first.
        turn_limit = AI_HISTORY_TURNS.get(tier, AI_HISTORY_TURNS["basic"])
        # Reply/DM chat passes its own context (the reply chain) as ready-made
        # {"role", "content"} messages instead of a per-user session.
        history = [] if history_override is not None else await get_ai_conversation_history(session_id, limit_turns=turn_limit)

        # Build conversation with context
        system_content = SYSTEM_PROMPT_ANIME if is_anime_question else SYSTEM_PROMPT_GENERAL
        if command_context:
            # Permission-filtered command list built by the caller (see
            # modules/command_reference.py + discord_bot/cogs/ai_tools.py) —
            # already scoped to what this specific user is allowed to run.
            system_content = f"{system_content}\n\n{command_context}"
        messages = [
            {"role": "system", "content": system_content}
        ]

        # Replay each past turn as a real user/assistant pair — no more
        # cutting prior prompts to 200 chars (that silently dropped context
        # mid-sentence); full stored text is already capped sanely at
        # MAX_STORED_TEXT on the way in, and the model has plenty of room.
        for hist in history:
            if hist.get('prompt'):
                messages.append({"role": "user", "content": hist['prompt']})
            if hist.get('response'):
                messages.append({"role": "assistant", "content": hist['response']})

        if history_override:
            messages.extend(history_override)

        # Add current message
        messages.append({"role": "user", "content": message})
        
        # Call Groq API
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": AI_CHAT_MODEL,
                "messages": messages,
                "temperature": 0.7,
                "max_completion_tokens": 400,
                "reasoning_effort": "low",
                "top_p": 1.0
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    msg = data.get('choices', [{}])[0].get('message', {})

                    raw_calls = msg.get('tool_calls') or []
                    if tools and raw_calls:
                        import json as _json
                        parsed = []
                        for tc in raw_calls:
                            fn = tc.get('function', {})
                            try:
                                args = _json.loads(fn.get('arguments') or '{}')
                            except (ValueError, TypeError):
                                args = {}
                            name = fn.get('name')
                            if name:
                                parsed.append({"name": name, "arguments": args})
                        if parsed:
                            return {"tool_calls": parsed}
                        # Model claimed a tool call but gave nothing usable —
                        # fall through and treat any content as normal text.

                    response_text = msg.get('content', '')
                    
                    response_text = trim_reply(response_text)
                    response_text = render_support_link(response_text)
                    response_text = scrub_raw_support_url(response_text)
                    response_text = scrub_giftboost_mention(response_text)
                    if mentions_other_bot(response_text):
                        response_text = OTHER_BOT_REFUSAL
                    if response_text:
                        # Log usage — store both sides of the turn plus the
                        # session so this exchange can be replayed as real
                        # history next time, not just remembered as a prompt.
                        await log_ai_usage(user_id, "messages", message, response_text=response_text,
                                           session_id=session_id, guild_id=guild_id, kind=kind)
                        return response_text
                else:
                    error = await resp.text()
                    ai_chat.last_error = f"Groq API HTTP {resp.status}: {error[:300]}"
                    logger.error(f"[v0] Groq API error: {error}")
        
        return None
    
    except Exception as e:
        ai_chat.last_error = f"{type(e).__name__}: {e}"
        logger.error(f"[v0] Error in ai_chat: {e}")
        return None


ai_chat.last_error = None  # set fresh on each call; see docstring above


# ═══════════════════════════════════════════════════════════════════════════
# AI IMAGE GENERATION
# ═══════════════════════════════════════════════════════════════════════════

async def generate_image(user_id: int, prompt: str, style: str = "anime") -> Optional[Dict]:
    """
    Generate an image from prompt. Tries, in order:
      1. Fal AI (FLUX Pro) — if FAL_API_KEY is set. Fast, good anime results.
      2. Gemini 2.5 Flash Image ("Nano Banana") — if GEMINI_API_KEY is set.
         Free Google AI Studio tier, no billing required (see
         https://aistudio.google.com/apikey to get a key).
      3. Pollinations.ai — no key needed at all, always available as a
         last-resort free fallback. Lower reliability/consistency than the
         above two (it's a public community service, not an SLA'd product),
         but means /aiimage never goes fully dead just because no key is set.

    NOTE: OpenAI DALL-E support was removed — DALL-E 2 and DALL-E 3 were
    both shut down by OpenAI on 2026-05-12 (see
    https://developers.openai.com/api/docs/deprecations); every call to
    them now fails outright, so keeping that code around was pure dead
    weight. If you want OpenAI's current model instead of Gemini, that's
    gpt-image-1 / gpt-image-1-mini — different request shape and returns
    base64 image bytes, not a URL (same as Gemini/Pollinations below).

    Returns dict with: EITHER "url" (Fal — a stable hosted image URL) OR
    "image_bytes" + "mime_type" (Gemini/Pollinations — raw bytes the caller
    must upload as a Discord file attachment, since these aren't backed by
    a stable public URL Discord can just embed). Always also includes
    "prompt", "model", "style".
    """
    try:
        # No top-level "not configured" short-circuit here: Pollinations
        # needs no key at all, so image generation should still work even
        # with zero keys configured (Fal/Gemini are just quality upgrades).
        
        # Clean and validate prompt
        prompt = prompt.strip()[:500]
        if not prompt or len(prompt) < 5:
            return {"error": "Prompt must be at least 5 characters."}
        
        # Add style prefix
        if style == "anime":
            full_prompt = f"anime style, {prompt}"
        elif style == "realistic":
            full_prompt = f"realistic, {prompt}"
        elif style == "3d":
            full_prompt = f"3d render, {prompt}"
        else:
            full_prompt = prompt
        
        if FAL_API_KEY:
            result = await _generate_image_fal(full_prompt)
            if result:
                await log_ai_usage(user_id, "images", prompt)
                return result

        if GEMINI_API_KEY:
            result = await _generate_image_gemini(full_prompt)
            if result:
                await log_ai_usage(user_id, "images", prompt)
                return result

        # Always-available fallback — no key required.
        result = await _generate_image_pollinations(full_prompt)
        if result:
            await log_ai_usage(user_id, "images", prompt)
            return result

        return {"error": "Image generation failed. Try again later."}
    
    except Exception as e:
        logger.error(f"[v0] Error in generate_image: {e}")
        return {"error": str(e)[:100]}


async def _generate_image_fal(prompt: str) -> Optional[Dict]:
    """Generate image using Fal AI (fast, anime-friendly)."""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Key {FAL_API_KEY}"}
            
            # Use Fal's FLUX model for anime-style images
            payload = {
                "prompt": prompt,
                "num_inference_steps": 20,
                "guidance_scale": 7.5,
                "image_size": "square"
            }
            
            async with session.post(
                "https://fal.run/fal-ai/flux-pro",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if data.get('images') and len(data['images']) > 0:
                        image_url = data['images'][0].get('url')
                        if image_url:
                            return {
                                "url": image_url,
                                "prompt": prompt,
                                "model": "fal-ai",
                                "generation_time_ms": 0,
                                "style": "anime"
                            }
        
        return None
    
    except Exception as e:
        logger.error(f"[v0] Fal image generation error: {e}")
        return None


async def _generate_image_gemini(prompt: str) -> Optional[Dict]:
    """Generate image using Gemini 2.5 Flash Image ("Nano Banana"). Free
    Google AI Studio tier — see GEMINI_API_KEY above. Unlike Fal/DALL-E,
    this always returns base64-encoded image bytes inline in the JSON
    response (no hosted URL), so the caller has to upload it as a file
    rather than just linking to it."""
    try:
        import base64
        async with aiohttp.ClientSession() as session:
            headers = {
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            }
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

            async with session.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),  # Google's own docs: complex prompts can take up to ~2 min
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[v0] Gemini image generation HTTP {resp.status}: {(await resp.text())[:300]}")
                    return None
                data = await resp.json()

            candidates = data.get("candidates") or []
            if not candidates:
                return None
            for part in (candidates[0].get("content") or {}).get("parts") or []:
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    return {
                        "image_bytes": base64.b64decode(inline["data"]),
                        "mime_type": inline.get("mimeType", "image/png"),
                        "prompt": prompt,
                        "model": "gemini-2.5-flash-image",
                        "style": "anime",
                    }
        return None

    except Exception as e:
        logger.error(f"[v0] Gemini image generation error: {e}")
        return None


async def _generate_image_pollinations(prompt: str) -> Optional[Dict]:
    """Last-resort free fallback — no API key required at all, so /aiimage
    never goes fully dead even with nothing configured. Pollinations.ai is
    a public community service (not an SLA'd product): quality and
    reliability are a step down from Fal/Gemini, and there's no support to
    escalate to if it's flaky, but it costs nothing and needs zero setup."""
    try:
        from urllib.parse import quote
        url = (
            f"https://image.pollinations.ai/prompt/{quote(prompt)}"
            f"?width=1024&height=1024&nologo=true&model=flux"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    logger.error(f"[v0] Pollinations image generation HTTP {resp.status}")
                    return None
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                if not content_type.startswith("image/"):
                    logger.error(f"[v0] Pollinations returned non-image content-type: {content_type}")
                    return None
                image_bytes = await resp.read()

        if not image_bytes:
            return None
        return {
            "image_bytes": image_bytes,
            "mime_type": content_type,
            "prompt": prompt,
            "model": "pollinations",
            "style": "anime",
        }

    except Exception as e:
        logger.error(f"[v0] Pollinations image generation error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# AI USAGE CHECKS
# ═══════════════════════════════════════════════════════════════════════════

async def check_ai_usage_limit(user_id: int, tier: str, usage_type: str = "messages") -> tuple[bool, str]:
    """
    Check if user has hit daily AI usage limit for their tier.
    Returns (is_allowed: bool, message: str)
    """
    try:
        from config import DISCORD_CLONE_ADMIN_IDS
        if user_id in DISCORD_CLONE_ADMIN_IDS:
            # Bot owner/admins bypass daily AI caps entirely, same
            # convention as image_search.py / media_connect.py / clone_admin.py.
            return (True, "")

        if tier not in AI_USAGE_CAPS:
            tier = "basic"
        
        cap = AI_USAGE_CAPS[tier].get("daily_messages" if usage_type == "messages" else "daily_images")
        usage = await get_user_ai_usage(user_id, usage_type)
        
        if usage >= cap:
            if usage_type == "messages":
                return (False, (
                    f"You've used all {cap} of today's free AI chats. They reset in {until_reset_text()} "
                    f"(midnight UTC). Nobody is paying for AI credits here — there's nothing to top up."
                ))
            return (False, (
                f"You've used all {cap} of today's AI images. They reset in {until_reset_text()} (midnight UTC)."
            ))
        
        # Warn if near limit
        if usage >= cap * 0.8:
            cap_name = "messages" if usage_type == "messages" else "images"
            return (True, f"⚠️ You're near your daily {cap_name} limit ({usage}/{cap})")
        
        return (True, "")
    
    except Exception as e:
        logger.error(f"[v0] Error checking AI usage: {e}")
        return (True, "")  # Allow by default if error


# ═══════════════════════════════════════════════════════════════════════════
# REPLY / DM CHAT — per-user-per-server daily cap (separate from the /aichat
# tier limits above). Counted by user id + server, across every channel in
# that server; DMs are counted on their own (guild_id IS NULL).
# ═══════════════════════════════════════════════════════════════════════════

REPLY_CAP_NORMAL = 10
REPLY_CAP_PREMIUM = 30
DM_CAP = 10


async def get_reply_usage(user_id: int, guild_id: Optional[int]) -> int:
    """Today's (UTC) reply-chat messages for this user in this server (or in DMs)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_chat_usage WHERE user_id = $1 AND kind = 'reply' "
                "AND guild_id IS NOT DISTINCT FROM $2 "
                "AND DATE(created_at) = (NOW() AT TIME ZONE 'UTC')::date",
                user_id, guild_id,
            )
        return count or 0
    except Exception as e:
        logger.error(f"[ai] Error getting reply usage: {e}")
        return 0


def reply_cap_for(guild_id: Optional[int], is_premium: bool) -> int:
    if guild_id is None:
        return DM_CAP
    return REPLY_CAP_PREMIUM if is_premium else REPLY_CAP_NORMAL


async def check_reply_limit(user_id: int, guild_id: Optional[int], is_premium: bool) -> tuple[bool, str]:
    """(allowed, cap-hit message). The message states plainly that Premium is a
    server subscription and nobody is paying for AI credits."""
    try:
        from config import DISCORD_CLONE_ADMIN_IDS
        if user_id in DISCORD_CLONE_ADMIN_IDS:
            return (True, "")
        cap = reply_cap_for(guild_id, is_premium)
        if await get_reply_usage(user_id, guild_id) < cap:
            return (True, "")
        reset = until_reset_text()
        if guild_id is None:
            return (False, (
                f"You've used your {cap} free AI chats for today in DMs (resets in {reset}). "
                f"Chatting in a Premium server gives 30/day. Premium is a server subscription — "
                f"nobody is paying for AI credits."
            ))
        if is_premium:
            return (False, f"You've used your {cap} AI chats for today in this server (resets in {reset}).")
        return (False, (
            f"You've used your {cap} free AI chats for today in this server (resets in {reset}). "
            f"Server Premium raises it to {REPLY_CAP_PREMIUM}/day — it's a server subscription, not AI credits "
            f"(nobody is paying for credits). Ask a server admin about Premium."
        ))
    except Exception as e:
        logger.error(f"[ai] Error checking reply limit: {e}")
        return (True, "")


async def is_reply_chat_enabled(guild_id: int) -> bool:
    """Per-server switch; on by default (no row = enabled)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT enabled FROM discord_ai_reply_config WHERE guild_id = $1", guild_id)
        return True if val is None else bool(val)
    except Exception as e:
        logger.error(f"[ai] Error reading reply-chat switch: {e}")
        return True


async def set_reply_chat_enabled(guild_id: int, enabled: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO discord_ai_reply_config (guild_id, enabled, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (guild_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW()",
            guild_id, enabled,
        )
