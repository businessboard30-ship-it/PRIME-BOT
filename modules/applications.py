# path: modules/applications.py

"""Application forms: admins build a form with `/application`, the bot posts a panel with an
Apply button, applicants answer in a modal, staff accept/decline in a review channel.

Free:    1 live form per server, 3 questions, 2 colours, accept/decline + DM to the applicant.
Premium: 10 live forms, 5 questions (Discord's modal limit), all 10 colours, auto-role on accept.
"""

import json
from typing import List, Optional

import discord

from database import db, get_pool

# (key, label, hex, premium_only) — the first two are free, all ten are Premium.
COLORS = [
    ("blurple", "Blurple", 0x5865F2, False),
    ("emerald", "Emerald", 0x2ECC71, False),
    ("crimson", "Crimson", 0xE74C3C, True),
    ("sunset", "Sunset", 0xE67E22, True),
    ("gold", "Gold", 0xF1C40F, True),
    ("rose", "Rose", 0xFF69B4, True),
    ("violet", "Violet", 0x9B59B6, True),
    ("ocean", "Ocean", 0x3498DB, True),
    ("mint", "Mint", 0x1ABC9C, True),
    ("slate", "Slate", 0x2C2F33, True),
]
COLOR_BY_KEY = {c[0]: c for c in COLORS}
_BUTTON_STYLE = {
    "blurple": discord.ButtonStyle.primary, "emerald": discord.ButtonStyle.success,
    "crimson": discord.ButtonStyle.danger,
}

MAX_QUESTIONS_FREE = 3
MAX_QUESTIONS_PREMIUM = 5
MAX_FORMS_FREE = 1
MAX_FORMS_PREMIUM = 10
QUESTION_MAX_LEN = 100


def colour_for(key: str) -> discord.Colour:
    return discord.Colour(COLOR_BY_KEY.get(key, COLORS[0])[2])


def button_style_for(key: str) -> discord.ButtonStyle:
    return _BUTTON_STYLE.get(key, discord.ButtonStyle.secondary)


def max_questions(premium: bool) -> int:
    return MAX_QUESTIONS_PREMIUM if premium else MAX_QUESTIONS_FREE


def max_forms(premium: bool) -> int:
    return MAX_FORMS_PREMIUM if premium else MAX_FORMS_FREE


async def is_premium(guild_id: int, clone_id) -> bool:
    try:
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False


def clean_emoji(text: str) -> Optional[str]:
    """Accepts a unicode emoji or a custom `<:name:id>`; returns None when it isn't one."""
    text = (text or "").strip()
    if not text:
        return None
    pe = discord.PartialEmoji.from_str(text)
    if pe.id:
        return text
    if len(text) <= 16 and not any(ch.isascii() and ch.isalnum() for ch in text):
        return text
    return None


def parse_questions(raw: str, premium: bool) -> List[str]:
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line:
            out.append(line[:QUESTION_MAX_LEN])
    return out[:max_questions(premium)]


def _form(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    q = d.get("questions")
    d["questions"] = json.loads(q) if isinstance(q, str) else (q or [])
    return d


async def create_form(guild_id: int, clone_id, creator_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO discord_application_forms (guild_id, clone_id, creator_id) VALUES ($1, $2, $3) RETURNING *",
            guild_id, clone_id, creator_id)
    return _form(row)


async def get_form(form_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return _form(await conn.fetchrow("SELECT * FROM discord_application_forms WHERE id = $1", form_id))


_EDITABLE = {"title", "description", "button_label", "button_emoji", "color_key", "questions",
             "post_channel_id", "review_channel_id", "accept_role_id", "status",
             "panel_channel_id", "panel_message_id"}


async def update_form(form_id: int, **fields) -> Optional[dict]:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _EDITABLE:
            raise ValueError(f"not editable: {k}")
        vals.append(json.dumps(v) if k == "questions" else v)
        sets.append(f"{k} = ${len(vals) + 1}" + ("::jsonb" if k == "questions" else ""))
    if not sets:
        return await get_form(form_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE discord_application_forms SET {', '.join(sets)}, updated_at = NOW() WHERE id = $1 RETURNING *",
            form_id, *vals)
    return _form(row)


async def count_active_forms(guild_id: int, clone_id, exclude_form_id: int = 0) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM discord_application_forms WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "AND status = 'active' AND id <> $3", guild_id, clone_id, exclude_form_id)


async def add_submission(form: dict, user_id: int, answers: List[dict]) -> Optional[dict]:
    """None when this person already has a pending application on this form."""
    import asyncpg
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO discord_application_submissions (form_id, guild_id, user_id, answers) "
                "VALUES ($1, $2, $3, $4::jsonb) RETURNING *",
                form["id"], form["guild_id"], user_id, json.dumps(answers))
    except asyncpg.UniqueViolationError:
        return None
    return dict(row)


async def set_review_message(sub_id: int, channel_id: int, message_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE discord_application_submissions SET review_channel_id = $2, review_message_id = $3 WHERE id = $1",
            sub_id, channel_id, message_id)


async def delete_submission(sub_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM discord_application_submissions WHERE id = $1", sub_id)


async def get_submission(sub_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM discord_application_submissions WHERE id = $1", sub_id)
    if not row:
        return None
    d = dict(row)
    a = d.get("answers")
    d["answers"] = json.loads(a) if isinstance(a, str) else (a or [])
    return d


async def decide(sub_id: int, accepted: bool, staff_id: int) -> Optional[dict]:
    """Atomic: only the first click on a still-pending application wins."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE discord_application_submissions SET status = $2, decided_by = $3, decided_at = NOW() "
            "WHERE id = $1 AND status = 'pending' RETURNING *",
            sub_id, "accepted" if accepted else "declined", staff_id)
    if not row:
        return None
    d = dict(row)
    a = d.get("answers")
    d["answers"] = json.loads(a) if isinstance(a, str) else (a or [])
    return d


async def list_forms(guild_id: int, clone_id) -> List[dict]:
    """Every non-draft form of a server (newest first) with how many applications await review."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT f.*, (SELECT COUNT(*) FROM discord_application_submissions s "
            "             WHERE s.form_id = f.id AND s.status = 'pending') AS pending "
            "FROM discord_application_forms f "
            "WHERE f.guild_id = $1 AND f.clone_id IS NOT DISTINCT FROM $2 AND f.status <> 'draft' "
            "ORDER BY f.id DESC LIMIT 25", guild_id, clone_id)
    return [_form(r) for r in rows]
