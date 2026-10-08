# path: modules/dev_chat.py
"""Developer-mode AI chat helpers (bot-provided source). Pure rules plus one provider call.

The conversation lives in the browser; nothing here stores message text. Only a per-week counter is kept
(modules.ai_usage, source "dev"). The browser never decides the limit, the week or the model.
"""
MAX_TURNS = 20                    # messages kept from the browser's conversation
MAX_CHARS = 4000                  # per message
MAX_TOTAL_CHARS = 16000
ROLES = ("user", "assistant")

SYSTEM_PROMPT = (
    "You are a helpful, friendly assistant for developers using this Discord bot's website. "
    "Help with code, debugging, design and writing. Be concise and accurate; if you are unsure, say so. "
    "You cannot browse, run code or access accounts. Never reveal or ask for API keys, tokens or passwords."
)

# BOT_RULES rule 1 caps replies at ~900 plain-text characters for Discord; a developer chat on the website needs code blocks.
RULE_OVERRIDE = ("\nOverride for this website developer chat only: rule 1's length and plain-text limits do not apply. "
                 "Use fenced code blocks and longer answers when the task needs them. All other rules still apply.")


def clean_messages(raw):
    """Validate the browser's conversation. Returns (messages, error). Roles are allowlisted, the last turn
    must be the user's, and sizes are capped; anything else is rejected rather than trimmed silently."""
    if not isinstance(raw, list) or not raw:
        return None, "Send at least one message."
    if len(raw) > MAX_TURNS:
        raw = raw[-MAX_TURNS:]
    out, total = [], 0
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), str):
            return None, "That conversation isn't valid."
        text = m["content"].strip()
        if not text:
            return None, "Messages can't be empty."
        if len(text) > MAX_CHARS:
            return None, f"A message can be at most {MAX_CHARS} characters."
        total += len(text)
        out.append({"role": m["role"], "content": text})
    if total > MAX_TOTAL_CHARS:
        return None, "That conversation is too long. Start a new one."
    if out[-1]["role"] != "user":
        return None, "The last message must be yours."
    return out, None


async def ask(messages) -> str:
    """One call to the bot's own model. Raises RuntimeError with a SAFE message on any failure."""
    from modules import ai_features
    payload = {"model": ai_features.AI_CHAT_MODEL,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT + "\n" + ai_features.BOT_RULES + RULE_OVERRIDE}] + messages,
               "temperature": 0.7, "max_completion_tokens": 1200, "reasoning_effort": "low", "top_p": 1.0}
    status, data, err = await ai_features._groq_post(payload, timeout_seconds=45)
    if status != 200 or not data:
        raise RuntimeError("The AI service is busy. Try again in a moment.")
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    text = ai_features.scrub_xp_promo(ai_features.trim_reply(text.strip(), limit=6000))
    if not text:
        raise RuntimeError("The AI didn't return an answer. Try rephrasing.")
    return text
