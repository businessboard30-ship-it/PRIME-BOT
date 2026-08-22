"""
AI Store — runs one turn of a buyer's conversation.

Money-safety invariant: the provider is only ever called AFTER a pre-flight
balance check, and the wallet is only ever debited AFTER a successful
response with real usage numbers. A provider/network failure during the
call never reaches a debit — chatFlow's Node twin has the identical
ordering, kept in sync deliberately.
"""

import logging
from typing import Optional

from database import db, InsufficientCreditsError
from modules.ai_store_providers import chat, preflight_estimate, estimate_margin, rough_token_count

logger = logging.getLogger(__name__)


async def run_chat_turn(user_id: int, session_id: int, provider: str, model: str, user_text: str,
                         listing_id: Optional[int] = None) -> dict:
    history = await db.ai_store_get_messages(session_id)
    messages = list(history) + [{"role": "user", "content": user_text}]

    # If this session is chatting with a seller's listed persona, prepend
    # its system prompt. The API key used underneath is always the
    # platform's own — listings only shape the prompt, never credentials.
    if listing_id:
        listing = await db.ai_store_get_listing(listing_id)
        if listing and listing.get("system_prompt"):
            messages.insert(0, {
                "role": "user",
                "content": f"[System instructions for this assistant: {listing['system_prompt']}]",
            })

    input_token_estimate = sum(rough_token_count(m["content"]) for m in messages)
    estimate = preflight_estimate(provider, model, input_token_estimate)

    from config import DISCORD_CLONE_ADMIN_IDS
    is_owner = user_id in DISCORD_CLONE_ADMIN_IDS

    if not is_owner:
        balance = await db.ai_store_get_balance(user_id)
        if balance < estimate:
            raise InsufficientCreditsError(estimate, balance)

    # Call the provider. If this raises, nothing has been charged yet.
    result = await chat(provider, model, messages)

    # Debit the REAL cost from actual usage. If balance somehow dropped
    # between preflight and now (concurrent request), this raises
    # InsufficientCreditsError — the reply was generated but never
    # persisted/charged, so no partial charge ever lands. Discord clone
    # admins (DISCORD_CLONE_ADMIN_IDS) are never charged — the platform
    # still eats the real provider cost, same as any other owner-bypass
    # feature in this codebase, so use sparingly.
    if is_owner:
        balance_after = await db.ai_store_get_balance(user_id)
    else:
        balance_after = await db.ai_store_debit_credits(
            user_id, result["cost_credits"],
            meta={"session_id": session_id, "provider": provider, "model": model,
                  "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]},
        )

    await db.ai_store_add_message(session_id, "user", user_text)
    assistant_message_id = await db.ai_store_add_message(
        session_id, "assistant", result["text"], result["input_tokens"], result["output_tokens"], result["cost_credits"]
    )

    if listing_id:
        await db.ai_store_increment_uses(listing_id)
        # Margin tracked here for future profitability reporting even
        # though revenue share is off — estimate_margin never fails loudly.
        try:
            estimate_margin(provider, model, result["input_tokens"], result["output_tokens"])
        except Exception:
            pass

    return {
        "text": result["text"],
        "cost_credits": result["cost_credits"],
        "balance_after": balance_after,
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "message_id": assistant_message_id,
    }
