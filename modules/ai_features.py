"""
AI Features Module — Conversational AI Chat & Image Generation
Uses Groq API for chat (anime questions, general chat)
Uses Fal AI or Replicate for image generation
Gated behind premium tier system (superbot_adapter.get_user_tier)
"""

import aiohttp
import logging
from typing import Optional, List, Dict
from datetime import datetime
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

AI_CHAT_MODEL = "openai/gpt-oss-120b"  # Groq's current recommended general-purpose model
SYSTEM_PROMPT_ANIME = (
    "You are an anime expert and helpful assistant. Provide friendly, concise responses about anime, manga, characters, and recommendations. "
    "Keep answers under 500 characters when possible. Be conversational and engaging."
)
SYSTEM_PROMPT_GENERAL = (
    "You are a helpful, friendly assistant. Provide concise, accurate responses to questions. "
    "Keep answers under 500 characters when possible."
)

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
            today = datetime.now().date()
            
            table = "ai_chat_usage" if usage_type == "messages" else "ai_image_usage"
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = $1 AND DATE(created_at) = $2",
                user_id, today
            )
        return count or 0
    except Exception as e:
        logger.error(f"[v0] Error getting AI usage: {e}")
        return 0


async def log_ai_usage(user_id: int, usage_type: str = "messages", prompt_text: str = "",
                        response_text: str = None, session_id: int = None) -> bool:
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
                if usage_type == "messages":
                    await conn.execute(
                        "INSERT INTO ai_chat_usage (user_id, prompt, response, session_id, created_at) "
                        "VALUES ($1, $2, $3, $4, NOW())",
                        user_id, prompt_text[:MAX_STORED_TEXT],
                        response_text[:MAX_STORED_TEXT] if response_text else None,
                        session_id,
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
                   command_context: Optional[str] = None) -> Optional[str]:
    """
    Send message to Groq API and get response.
    Returns response text on success. On failure, returns None (caller shows
    a generic "AI service error" to the user) but ALSO stashes the real
    error string on `ai_chat.last_error` so an admin-facing surface (see
    handlers/ai_handler.py) can show the actual cause — missing key, bad
    model name, Groq outage, rate limit, etc. — instead of just "None".

    `session_id` scopes conversation history to one active conversation
    (see get_or_create_active_session / /newchat /endchat in
    discord_bot/cogs/ai_tools.py) — without it, this behaves like a single
    one-shot turn with no prior context. `tier` controls how many past
    turns are replayed (AI_HISTORY_TURNS), a founder/elite perk.
    """
    ai_chat.last_error = None
    try:
        if not GROQ_API_KEY:
            ai_chat.last_error = "GROQ_API_KEY is not set in the environment."
            return "⚠️ AI service not configured. Admin needs to set GROQ_API_KEY."

        # Get conversation history for this specific session, both sides of
        # each turn (not just past user prompts), oldest first.
        turn_limit = AI_HISTORY_TURNS.get(tier, AI_HISTORY_TURNS["basic"])
        history = await get_ai_conversation_history(session_id, limit_turns=turn_limit)

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
                "max_completion_tokens": 600,
                "reasoning_effort": "low",
                "top_p": 1.0
            }
            
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response_text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                    
                    if response_text:
                        # Log usage — store both sides of the turn plus the
                        # session so this exchange can be replayed as real
                        # history next time, not just remembered as a prompt.
                        await log_ai_usage(user_id, "messages", message, response_text=response_text, session_id=session_id)
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
            cap_name = "messages" if usage_type == "messages" else "image"
            return (False, f"Daily {cap_name} limit reached ({usage}/{cap}). Upgrade your tier or try tomorrow.")
        
        # Warn if near limit
        if usage >= cap * 0.8:
            cap_name = "messages" if usage_type == "messages" else "images"
            return (True, f"⚠️ You're near your daily {cap_name} limit ({usage}/{cap})")
        
        return (True, "")
    
    except Exception as e:
        logger.error(f"[v0] Error checking AI usage: {e}")
        return (True, "")  # Allow by default if error
