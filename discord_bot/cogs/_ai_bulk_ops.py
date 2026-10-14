# path: discord_bot/cogs/_ai_bulk_ops.py

"""
Discord-side half of the AI bulk channel commands (the pure planning lives in modules/ai_bulk.py).

Everything here runs ONE Discord call at a time with a pause in between, never in parallel, and stops at the first
failure (missing permission, Discord refusing, a call that hangs) instead of retrying -- so a confused or
over-eager request can never become a burst of API calls.
"""

import asyncio
import logging
import re
from typing import List, Optional, Tuple

import discord

from modules import ai_bulk
from modules.text_styles import channel_name, plain_name, wrap_brackets

logger = logging.getLogger(__name__)

KINDS = ("text", "voice", "category")
GUILD_CHANNEL_LIMIT = 500
_DELETABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel,
              discord.CategoryChannel)


def styled_name(base: str, font_key: Optional[str], bracket: Optional[str], kind: str) -> str:
    """The final channel name: `base` (already a plain slug) in the chosen font and bracket."""
    if font_key:
        return channel_name(base, font_key, hyphenate=(kind == "text"), bracket=bracket, lower=(kind != "category"))
    return wrap_brackets(base, bracket)[:100]


def existing_names(guild: discord.Guild, kind: str, category: Optional[discord.CategoryChannel]) -> List[str]:
    """Plain names already taken where the new channels would go (so a second 'general' isn't made)."""
    if kind == "category":
        pool = guild.categories
    else:
        pool = category.channels if category is not None else guild.channels
    return [plain_name(c.name) for c in pool if not isinstance(c, discord.CategoryChannel) or kind == "category"]


def find_category(guild: discord.Guild, value) -> Tuple[Optional[discord.CategoryChannel], Optional[str]]:
    """(category, problem). An empty value means 'no category' and is not a problem."""
    if value in (None, ""):
        return None, None
    raw = str(value).strip()
    m = re.fullmatch(r"<#(\d+)>|(\d{15,25})", raw)
    if m:
        ch = guild.get_channel(int(m.group(1) or m.group(2)))
        if isinstance(ch, discord.CategoryChannel):
            return ch, None
    want = plain_name(raw).lower().strip(" -")
    hits = [c for c in guild.categories if c.name.lower() == raw.lower() or plain_name(c.name).lower().strip(" -") == want]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"I couldn't find a category called \u201c{raw}\u201d."
    return None, f"More than one category matches \u201c{raw}\u201d -- use its exact name."


def resolve_channels(guild: discord.Guild, names: List[str], current=None) -> dict:
    """Matches each asked-for name to ONE real channel. Anything unclear is reported, never guessed.
    Returns {"found": [...], "missing": [...], "ambiguous": [...], "protected": [(channel, why)]}."""
    out = {"found": [], "missing": [], "ambiguous": [], "protected": []}
    guarded = {}
    for attr, why in (("rules_channel", "it's the community rules channel"),
                      ("public_updates_channel", "it's the community updates channel"),
                      ("system_channel", "it's the server's system messages channel"),
                      ("afk_channel", "it's the AFK voice channel")):
        ch = getattr(guild, attr, None)
        if ch is not None:
            guarded[ch.id] = why
    if current is not None:
        guarded[current.id] = "it's the channel we're chatting in"
    chosen = set()
    for raw in names:
        m = re.fullmatch(r"<#(\d+)>|(\d{15,25})", raw.strip())
        if m:
            ch = guild.get_channel(int(m.group(1) or m.group(2)))
            hits = [ch] if isinstance(ch, _DELETABLE) else []
        else:
            low = raw.strip().lstrip("#").lower()
            plain = plain_name(low).strip(" -")
            pool = [c for c in guild.channels if isinstance(c, _DELETABLE)]
            hits = [c for c in pool if c.name.lower() == low]
            if not hits:
                hits = [c for c in pool if plain_name(c.name).lower().strip(" -") == plain]
        if not hits:
            out["missing"].append(raw)
        elif len(hits) > 1:
            out["ambiguous"].append(raw)
        elif hits[0].id in guarded:
            out["protected"].append((hits[0], guarded[hits[0].id]))
        elif hits[0].id not in chosen:
            chosen.add(hits[0].id)
            out["found"].append(hits[0])
    # A category is only removed together with everything inside it if the user named all of that too.
    keep = []
    for ch in out["found"]:
        if isinstance(ch, discord.CategoryChannel) and any(c.id not in chosen for c in ch.channels):
            out["protected"].append((ch, "it still has channels in it you didn't ask me to delete"))
        else:
            keep.append(ch)
    out["found"] = keep
    return out


def _problem_text(res: dict) -> str:
    bits = []
    if res["missing"]:
        bits.append("not found: " + ai_bulk.summarize(res["missing"]))
    if res["ambiguous"]:
        bits.append("matches more than one channel (use #mention): " + ai_bulk.summarize(res["ambiguous"]))
    if res["protected"]:
        bits.append("left alone: " + ai_bulk.summarize(f"{c.name} ({why})" for c, why in res["protected"]))
    return "; ".join(bits)


def deletion_problems(res: dict) -> str:
    return _problem_text(res)


async def create_channels(guild: discord.Guild, names: List[str], kind: str,
                          category: Optional[discord.CategoryChannel], reason: str) -> Tuple[list, Optional[str]]:
    """Makes the channels one by one. Returns (created_channels, stop_reason_or_None)."""
    made: list = []
    for i, name in enumerate(names):
        if i:
            await asyncio.sleep(ai_bulk.PAUSE_SECONDS)
        try:
            if kind == "category":
                coro = guild.create_category(name, reason=reason)
            elif kind == "voice":
                coro = guild.create_voice_channel(name, category=category, reason=reason)
            else:
                coro = guild.create_text_channel(name, category=category, reason=reason)
            made.append(await asyncio.wait_for(coro, timeout=ai_bulk.CALL_TIMEOUT))
        except discord.Forbidden:
            return made, "I don't have **Manage Channels** here."
        except asyncio.TimeoutError:
            return made, "Discord is slow to answer (probably rate-limiting me), so I stopped."
        except discord.HTTPException as e:
            return made, f"Discord refused one of the names: {getattr(e, 'text', e)}"
    return made, None


async def delete_channels(channels: list, reason: str) -> Tuple[list, Optional[str]]:
    """Deletes the channels one by one, children before their category. Returns (deleted_names, stop_reason)."""
    ordered = sorted(channels, key=lambda c: isinstance(c, discord.CategoryChannel))
    gone: list = []
    for i, ch in enumerate(ordered):
        if i:
            await asyncio.sleep(ai_bulk.PAUSE_SECONDS)
        name = ch.name
        try:
            await asyncio.wait_for(ch.delete(reason=reason), timeout=ai_bulk.CALL_TIMEOUT)
            gone.append(name)
        except discord.Forbidden:
            return gone, f"I'm not allowed to delete **{name}**."
        except asyncio.TimeoutError:
            return gone, "Discord is slow to answer (probably rate-limiting me), so I stopped."
        except discord.HTTPException as e:
            return gone, f"Discord wouldn't delete **{name}**: {getattr(e, 'text', e)}"
    return gone, None


# ── roles ────────────────────────────────────────────────────────────────

def resolve_roles(guild: discord.Guild, names: List[str], asker: discord.Member) -> dict:
    """Matches each asked-for name to ONE real role the asker is allowed to delete. Anything unclear is reported,
    never guessed. Returns {"found": [...], "missing": [...], "ambiguous": [...], "protected": [(role, why)]}."""
    out = {"found": [], "missing": [], "ambiguous": [], "protected": []}
    me = guild.me
    chosen = set()
    for raw in names:
        m = re.fullmatch(r"<@&(\d+)>|(\d{15,25})", raw.strip())
        if m:
            r = guild.get_role(int(m.group(1) or m.group(2)))
            hits = [r] if r is not None else []
        else:
            low = raw.strip().lstrip("@").lower()
            if low in ("everyone", "here"):
                out["protected"].append((guild.default_role, "it's @everyone"))
                continue
            hits = [r for r in guild.roles if r.name.lower() == low]
        if not hits:
            out["missing"].append(raw)
            continue
        if len(hits) > 1:
            out["ambiguous"].append(raw)
            continue
        r = hits[0]
        if r.is_default():
            out["protected"].append((r, "it's @everyone"))
        elif r.managed:
            out["protected"].append((r, "it belongs to a bot or integration"))
        elif r >= me.top_role:
            out["protected"].append((r, "it's above my own top role"))
        elif asker.id != guild.owner_id and r >= asker.top_role:
            out["protected"].append((r, "it's at or above your own top role"))
        elif r.id not in chosen:
            chosen.add(r.id)
            out["found"].append(r)
    return out


def role_problems(res: dict) -> str:
    bits = []
    if res["missing"]:
        bits.append("not found: " + ai_bulk.summarize(res["missing"]))
    if res["ambiguous"]:
        bits.append("matches more than one role (use the exact name): " + ai_bulk.summarize(res["ambiguous"]))
    if res["protected"]:
        bits.append("left alone: " + ai_bulk.summarize(f"{r.name} ({why})" for r, why in res["protected"]))
    return "; ".join(bits)


async def create_roles(guild: discord.Guild, names: List[str], colour: Optional[int], hoist: bool,
                       mentionable: bool, reason: str) -> Tuple[list, Optional[str]]:
    """Makes the roles one by one with NO permissions (cosmetic only -- never Administrator). Returns
    (created_roles, stop_reason_or_None)."""
    made: list = []
    kwargs = dict(permissions=discord.Permissions.none(), hoist=bool(hoist), mentionable=bool(mentionable),
                  reason=reason)
    if colour is not None:
        kwargs["colour"] = discord.Colour(colour)
    for i, name in enumerate(names):
        if i:
            await asyncio.sleep(ai_bulk.PAUSE_SECONDS)
        try:
            made.append(await asyncio.wait_for(guild.create_role(name=name, **kwargs), timeout=ai_bulk.CALL_TIMEOUT))
        except discord.Forbidden:
            return made, "I don't have **Manage Roles** here."
        except asyncio.TimeoutError:
            return made, "Discord is slow to answer (probably rate-limiting me), so I stopped."
        except discord.HTTPException as e:
            return made, f"Discord refused one of the names: {getattr(e, 'text', e)}"
    return made, None


async def delete_roles(roles: list, reason: str) -> Tuple[list, Optional[str]]:
    """Deletes the roles one by one. Returns (deleted_names, stop_reason_or_None)."""
    gone: list = []
    for i, role in enumerate(roles):
        if i:
            await asyncio.sleep(ai_bulk.PAUSE_SECONDS)
        name = role.name
        try:
            await asyncio.wait_for(role.delete(reason=reason), timeout=ai_bulk.CALL_TIMEOUT)
            gone.append(name)
        except discord.Forbidden:
            return gone, f"I'm not allowed to delete **{name}**."
        except asyncio.TimeoutError:
            return gone, "Discord is slow to answer (probably rate-limiting me), so I stopped."
        except discord.HTTPException as e:
            return gone, f"Discord wouldn't delete **{name}**: {getattr(e, 'text', e)}"
    return gone, None
