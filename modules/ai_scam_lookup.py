# path: modules/ai_scam_lookup.py

"""
AI scam lookup: an admin gives the AI a Discord user id and the bot answers, in public, with the scam evidence it has RECORDED.

Not a slash command: it is an AI tool (like the choice tool) handled in discord_bot/cogs/ai_tools.py, offered and executed only for
people with REQUIRED_PERMISSION in the server. The report is built by this code straight from the database -- the model never
writes or paraphrases the evidence, so it cannot invent any.

Evidence used (nothing new is collected or stored):
  * scam_shield_hits for THIS server: when, which channel, what kind of catch, the matched rule + its note, whether the message
    was deleted, and the stored text snippet (only while it is still retained; older rows have it scrubbed by the privacy policy);
  * other servers: counts / kinds / last time only (modules/scam_reputation.py) -- never their names or message text, exactly as
    the join-time heads-up already works, so one server's moderation is not exposed to another;
  * this server's own warns for the user;
  * facts Discord itself provides: account age (from the id), joined, banned here or not, timed out.

It is a heads-up for admins, not a verdict: accounts get hacked, and the report says so.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord

logger = logging.getLogger(__name__)

TOOL_NAME = "scam_lookup"
REQUIRED_PERMISSION = "administrator"     # "only admins": change this one word to loosen/tighten who may ask
COOLDOWN_SECONDS = 8
MAX_HITS_SHOWN = 6
SNIPPET_CHARS = 160
MAX_EVIDENCE_FETCH = 3      # archived copies opened per lookup (newest first)
MAX_LINKS_PER_HIT = 2
MAX_EXTRA_IMAGE_EMBEDS = 2  # the main embed shows the first image; up to this many more get their own
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>`\"')\]]+")

KIND_LABELS = {"word": "scam wording", "domain": "scam link", "image": "known scam image", "vision": "AI-detected scam image"}

_ID_RE = re.compile(r"^<@!?(\d{15,20})>$|^(\d{15,20})$")
_last: Dict[int, float] = {}


def tool_schema() -> dict:
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": ("ADMIN ONLY. Look up recorded SCAM evidence (scam shield catches, warns, account facts) for a Discord USER ID "
                        "and post the findings publicly. Use when an admin asks if a user / scammer / user id is a scam or has "
                        "scam history or evidence."),
        "parameters": {"type": "object",
                       "properties": {"user_id": {"type": "string", "description": "The Discord user ID (digits) or an @mention"}},
                       "required": ["user_id"]}}}


def parse_user_id(value) -> Optional[int]:
    m = _ID_RE.match(str(value or "").strip())
    return int(m.group(1) or m.group(2)) if m else None


def on_cooldown(user_id: int, now: Optional[float] = None) -> int:
    now = time.monotonic() if now is None else now
    last = _last.get(user_id)
    if last is not None and now - last < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - (now - last)) + 1
    if len(_last) > 5000:
        _last.clear()
    _last[user_id] = now
    return 0


def defang(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Makes stored scam text safe to show publicly: no clickable links, no pings, no markdown tricks."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"(?i)https?://", lambda m: "hxxp" + ("s" if m.group(0).lower().startswith("https") else "") + "://", t)
    t = re.sub(r"(?i)\b([a-z0-9-]+)\.([a-z]{2,})\b", r"\1[.]\2", t)
    t = t.replace("@", "@\u200b").replace("`", "'").replace("<", "\u2039").replace(">", "\u203a")
    t = re.sub(r"([*_~|\\])", r"\\\1", t)
    return t[:limit] + ("\u2026" if len(t) > limit else "")


async def _pool():
    from database import get_pool
    return await get_pool()


async def gather(guild_id: int, user_id: int) -> dict:
    """Everything recorded about this user. A failure of one source is reported as missing, never raised."""
    out = {"here_total": 0, "hits": [], "other": None, "warn_total": 0, "warns": [], "errors": []}
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            out["here_total"] = int(await conn.fetchval(
                "SELECT COUNT(*) FROM scam_shield_hits WHERE guild_id = $1 AND user_id = $2", guild_id, user_id) or 0)
            rows = await conn.fetch(
                "SELECT h.id, h.channel_id, h.kind, h.matched, h.snippet, h.deleted, h.created_at, r.note, "
                "h.evidence_channel_id, h.evidence_message_id "
                "FROM scam_shield_hits h LEFT JOIN scam_shield_rules r ON r.id = h.rule_id "
                "WHERE h.guild_id = $1 AND h.user_id = $2 ORDER BY h.id DESC LIMIT $3", guild_id, user_id, MAX_HITS_SHOWN)
            out["hits"] = [dict(r) for r in rows]
    except Exception:
        logger.exception("[ai_scam_lookup] scam hits unavailable")
        out["errors"].append("scam catches")
    try:
        from modules import scam_reputation
        out["other"] = await scam_reputation.lookup(user_id, guild_id)
    except Exception:
        logger.exception("[ai_scam_lookup] cross-server reputation unavailable")
        out["errors"].append("other-server catches")
    try:
        from database import db
        warns, total = await db.dash_mod_warns(guild_id, user_id, 5)
        out["warns"], out["warn_total"] = warns, total
    except Exception:
        logger.exception("[ai_scam_lookup] warns unavailable")
        out["errors"].append("warns")
    return out


async def load_evidence(bot, guild_id: int, user_id: int, hits: List[dict]) -> Dict[int, dict]:
    """hit id -> {"links": [defanged urls], "images": [urls], "gone": bool} read from the archived copy Scam Shield saved.
    The copy lives in the shared evidence channel, so it is only trusted when its header names THIS server and THIS user.
    Best effort: anything unreachable just means no link/image for that catch."""
    out: Dict[int, dict] = {}
    if bot is None:
        return out
    opened = 0
    for h in hits:
        cid, mid = h.get("evidence_channel_id"), h.get("evidence_message_id")
        if not (cid and mid) or opened >= MAX_EVIDENCE_FETCH:
            continue
        opened += 1
        try:
            ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
            msg = await asyncio.wait_for(ch.fetch_message(int(mid)), timeout=5)
        except Exception:
            logger.debug("[ai_scam_lookup] evidence copy for hit %s unreachable", h.get("id"), exc_info=True)
            out[h["id"]] = {"links": [], "images": [], "gone": True}
            continue
        text = getattr(msg, "content", "") or ""
        if f"server `{guild_id}`" not in text or f"user `{user_id}`" not in text:
            continue                                    # not this server's / this user's copy: never show it
        links, seen = [], set()
        for u in _URL_RE.findall(text):
            k = u.lower().rstrip(".,")
            if k not in seen:
                seen.add(k)
                links.append(defang(u.rstrip(".,"), 90))
            if len(links) >= MAX_LINKS_PER_HIT:
                break
        images = []
        for a in getattr(msg, "attachments", None) or []:
            ctype = (getattr(a, "content_type", "") or "").lower()
            if ctype.startswith("image/") or re.search(r"(?i)\.(png|jpe?g|gif|webp)$", getattr(a, "filename", "") or ""):
                images.append(a.url)
        out[h["id"]] = {"links": links, "images": images[:1], "gone": False}
    return out


def image_embeds(data: dict, evidence: Optional[Dict[int, dict]]) -> List[discord.Embed]:
    """Extra embeds (one image each) for catches beyond the first image, which the main embed already shows."""
    extra = []
    for h in data["hits"]:
        imgs = ((evidence or {}).get(h["id"]) or {}).get("images") or []
        if imgs:
            extra.append((h["id"], imgs[0]))
    out = []
    for hid, url in extra[1:1 + MAX_EXTRA_IMAGE_EMBEDS]:
        e = discord.Embed(title=f"Scam image \u2014 catch #{hid}", color=0xB85450)
        e.set_image(url=url)
        out.append(e)
    return out


def assess(data: dict) -> Tuple[str, str]:
    """(level, why). Deterministic from recorded counts -- no guessing."""
    here, other = data["here_total"], (data.get("other") or {}).get("catches", 0)
    if here >= 2 or (here >= 1 and other >= 1):
        return "strong", "Repeated recorded catches" + (" here and in other servers" if other else " in this server")
    if here == 1 or other >= 1:
        return "some", "A recorded catch " + ("in this server" if here else "in another server")
    return "none", "No scam catches are recorded for this user id"


_COLOR = {"strong": 0xB85450, "some": 0xC9A227, "none": 0x5B8C5A}
_LABEL = {"strong": "Strong evidence", "some": "Some evidence", "none": "No recorded evidence"}


def build_embed(user_id: int, data: dict, facts: dict, evidence: Optional[Dict[int, dict]] = None) -> discord.Embed:
    level, why = assess(data)
    evidence = evidence or {}
    e = discord.Embed(title=f"\U0001f50e Scam lookup \u2014 {_LABEL[level]}", color=_COLOR[level],
                      description=f"User ID `{user_id}`" + (f" ({defang(facts['display'], 60)})" if facts.get("display") else "")
                                  + f"\n**{why}.**")
    created = facts.get("created_at") or discord.utils.snowflake_time(user_id)
    age_days = max(0, (datetime.now(timezone.utc) - created).days)
    line = [f"Account created <t:{int(created.timestamp())}:R> ({age_days} days old)"]
    line.append("In this server" + (f", joined <t:{int(facts['joined_at'].timestamp())}:R>" if facts.get("joined_at") else "")
                if facts.get("in_server") else "Not currently in this server")
    if facts.get("banned") is True:
        line.append("**Banned here**")
    if facts.get("timed_out"):
        line.append("Currently timed out")
    e.add_field(name="Account", value="\n".join(line), inline=False)

    if data["hits"]:
        lines = []
        for h in data["hits"]:
            when = f"<t:{int(h['created_at'].timestamp())}:f>" if h.get("created_at") else "unknown time"
            kind = KIND_LABELS.get(h.get("kind"), "scam pattern")
            bits = [f"**#{h['id']}** {when}", f"{kind}"]
            if h.get("channel_id"):
                bits.append(f"<#{h['channel_id']}>")
            if h.get("matched"):
                bits.append(f"matched `{defang(h['matched'], 40).replace(chr(92), '')}`")
            bits.append("message deleted" if h.get("deleted") else "message kept")
            row = " \u00b7 ".join(bits)
            if h.get("note"):
                row += f"\n  rule: {defang(h['note'], 80)}"
            ev = evidence.get(h["id"]) or {}
            links = list(ev.get("links") or [])
            if not links and h.get("kind") == "domain" and h.get("matched"):
                links = [defang(h["matched"], 90).replace(chr(92), "")]     # the domain the rule matched
            if links:
                row += "\n  scam link: " + " , ".join(f"`{l.replace(chr(96), chr(39))}`" for l in links) + " (made unclickable)"
            if h.get("snippet"):
                row += "\n  > " + defang(h["snippet"])
            elif not links:
                row += "\n  > (text no longer stored)"
            if ev.get("images"):
                row += "\n  scam image: shown below"
            elif ev.get("gone"):
                row += "\n  scam image: the saved copy is no longer available"
            lines.append(row)
        more = data["here_total"] - len(data["hits"])
        e.add_field(name=f"Catches in this server ({data['here_total']})",
                    value=("\n".join(lines) + (f"\n\u2026and {more} older" if more > 0 else ""))[:1020], inline=False)
    else:
        e.add_field(name="Catches in this server", value="None recorded.", inline=False)

    other = data.get("other")
    if other:
        kinds = ", ".join(other.get("kinds") or []) or "scam patterns"
        last = f", last <t:{int(other['last_at'].timestamp())}:R>" if other.get("last_at") else ""
        e.add_field(name="Other servers (counts only)",
                    value=f"{other['catches']} catch(es) across {other['servers']} server(s): {kinds}{last}.", inline=False)
    else:
        e.add_field(name="Other servers (counts only)", value="None recorded.", inline=False)

    if data["warn_total"]:
        w = [f"#{x['id']} <t:{int(x['created_at'].timestamp())}:d> \u2014 {defang(x.get('reason') or 'no reason', 80)}"
             for x in data["warns"] if x.get("created_at")]
        e.add_field(name=f"Warns in this server ({data['warn_total']})", value="\n".join(w)[:1000] or "\u2014", inline=False)

    for h in data["hits"]:
        imgs = (evidence.get(h["id"]) or {}).get("images") or []
        if imgs:
            e.set_image(url=imgs[0])
            break

    foot = "A heads-up, not a verdict: accounts can be hacked. Only catches this bot recorded are shown."
    if data["errors"]:
        foot += " Couldn't read: " + ", ".join(data["errors"]) + "."
    e.set_footer(text=foot[:2000])
    return e
