# path: modules/image_host.py
"""The image-hosting channel (custom welcome backgrounds, ad images): one place that finds it, tests it and reports problems.

Every bot (the main bot and every clone) uses the same channel, in the support server. The owner can still point it
somewhere else with `/admin hostingchannel` (stored as the global setting `image_host_channel_id`), but a bot that cannot
reach that channel falls back to the default (config.IMAGE_HOST_CHANNEL_ID), so one unreachable channel can no longer
silently turn every custom background into the stock card.
"""
import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Optional

import discord

logger = logging.getLogger(__name__)

ALERT_COOLDOWN_SECONDS = 6 * 3600
_alert_sent: dict = {}
_startup_done = False
_jobs_started = False

MOVE_PER_RUN = 50                  # re-hosted backgrounds per run (the rest wait for the next run)
MOVE_PAUSE_SECONDS = 1.0           # between uploads, to stay well inside Discord's rate limits
MOVE_EVERY_SECONDS = 12 * 3600
NOTIFY_SERVER_OWNERS = True        # tell a server's owner when its background can't be moved (at most weekly per server)
SERVER_NOTICE_COOLDOWN_DAYS = 7
MAX_SERVER_NOTICES_PER_RUN = 10


@dataclass
class Check:
    name: str
    ok: Optional[bool]          # True ok, False problem, None skipped / info
    detail: str = ""
    fix: str = ""


async def candidates() -> list:
    """[(channel_id, source), ...] in the order they are tried: the owner's setting first, then the default."""
    import config
    out = []
    try:
        from database import db
        raw = await db.get_global_setting("image_host_channel_id")
        if raw and str(raw).isdigit() and int(raw) > 0:
            out.append((int(raw), "owner setting (/admin hostingchannel)"))
    except Exception:
        logger.warning("image host: couldn't read the owner setting", exc_info=True)
    default = int(getattr(config, "IMAGE_HOST_CHANNEL_ID", 0) or 0)
    if default and all(default != c for c, _ in out):
        out.append((default, "default (IMAGE_HOST_CHANNEL_ID)"))
    return out


async def open_channel(bot, channel_id: int):
    """(channel, None) when THIS bot can use the id as a text channel, else (None, plain-words reason)."""
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None, "that channel doesn't exist, or this bot is not in its server"
        except discord.Forbidden:
            return None, "this bot has no access to that channel"
        except discord.HTTPException as e:
            return None, f"Discord refused the lookup (HTTP {getattr(e, 'status', '?')})"
    if not isinstance(ch, discord.TextChannel):
        return None, "that id is not a normal text channel"
    return ch, None


async def resolve_channel(bot):
    """The first candidate channel THIS bot can reach, or None."""
    for cid, _src in await candidates():
        ch, _why = await open_channel(bot, cid)
        if ch is not None:
            return ch
    return None


def _me(channel, bot):
    g = channel.guild
    return g.get_member(bot.user.id) or g.me


def _test_png() -> bytes:
    from PIL import Image, ImageDraw
    from modules import welcome_card as wc
    img = Image.new("RGB", (wc.CARD_WIDTH, wc.CARD_HEIGHT), (43, 45, 49))
    d = ImageDraw.Draw(img)
    for x in range(0, wc.CARD_WIDTH, 30):
        d.rectangle([x, 0, x + 14, wc.CARD_HEIGHT], fill=(60 + x % 120, 70, 140))
    d.text((20, 20), "image host test", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _fetch_bytes(url: str):
    """(bytes, None) or (None, reason): the SAME strict download the welcome card uses for a saved background."""
    import aiohttp
    from discord_bot.cogs import welcome as welcome_cog
    async with aiohttp.ClientSession() as session:
        return await welcome_cog._fetch_custom_bg_bytes(session, url)


async def _render_card(background: bytes):
    """A welcome card drawn on `background` by the real renderer (PNG bytes), run off the event loop."""
    from modules import welcome_card as wc
    avatar = await asyncio.to_thread(_test_png)
    card, _fmt = await asyncio.to_thread(wc.render_welcome_card, avatar, "Test Member", "Member #1", guild_name="Test server",
                                         custom_background_bytes=background)
    return card


async def diagnose(bot, *, post_test: bool = True):
    """Run every step the custom-background path needs. Returns (checks, test_card_bytes_or_None). Never raises."""
    checks: list = []
    card = None
    cands = await candidates()
    if not cands:
        checks.append(Check("Channel id", False, "no hosting channel is configured",
                            "set IMAGE_HOST_CHANNEL_ID or press Set hosting channel"))
        return checks, None
    checks.append(Check("Channel id", True, ", ".join(f"`{c}` ({s})" for c, s in cands)))

    channel = None
    for cid, src in cands:
        ch, why = await open_channel(bot, cid)
        if ch is not None:
            channel = ch
            checks.append(Check("Channel reachable", True,
                                f"`{cid}` ({src}) is #{ch.name} in {ch.guild.name}" + (" (a fallback was used)" if cid != cands[0][0] else "")))
            break
        checks.append(Check("Channel reachable", False, f"`{cid}` ({src}): {why}",
                            "invite this bot to the support server and give it access to the channel"))
    if channel is None:
        return checks, None

    me = _me(channel, bot)
    perms = channel.permissions_for(me) if me else None
    if perms is None:
        checks.append(Check("Permissions", False, "couldn't read this bot's permissions there", "re-invite the bot to that server"))
    else:
        need = {"view_channel": "View Channel", "send_messages": "Send Messages", "attach_files": "Attach Files",
                "read_message_history": "Read Message History"}
        missing = [label for attr, label in need.items() if not getattr(perms, attr, False)]
        checks.append(Check("Permissions", not missing, "all four needed permissions are on" if not missing else "missing: " + ", ".join(missing),
                            "" if not missing else f"give this bot those permissions in #{channel.name}"))
        if missing:
            return checks, None

    if not post_test:
        return checks, None

    posted = None
    try:
        posted = await channel.send(content="Image hosting test (safe to delete)",
                                    file=discord.File(io.BytesIO(await asyncio.to_thread(_test_png)), filename="image-host-test.png"))
        checks.append(Check("Upload", True, "a test image was posted"))
    except discord.Forbidden:
        checks.append(Check("Upload", False, "Discord refused the upload (Forbidden)", f"allow Send Messages and Attach Files in #{channel.name}"))
    except discord.HTTPException as e:
        checks.append(Check("Upload", False, f"Discord refused the upload (HTTP {getattr(e, 'status', '?')})", "try again; if it repeats, the channel may be full or rate limited"))
    except Exception as e:
        checks.append(Check("Upload", False, f"the test image could not be made or sent ({type(e).__name__})", "check the bot logs"))
    if posted is None:
        return checks, None

    data = None
    try:
        again = await channel.fetch_message(posted.id)
        url = again.attachments[0].url if again.attachments else ""
        if not url:
            checks.append(Check("Read back", False, "the test message has no attachment when read again", "check the bot logs"))
        else:
            data, reason = await _fetch_bytes(url)
            checks.append(Check("Read back", data is not None, "the image downloaded fine" if data else f"the image could not be read back: {reason}",
                                "" if data else "Discord's file links may be blocked from this server; check the network settings"))
    except Exception as e:
        checks.append(Check("Read back", False, f"the test message could not be read again ({type(e).__name__})", "give the bot Read Message History"))

    if data:
        try:
            card = await _render_card(data)
            checks.append(Check("Welcome card", bool(card), "a welcome card was drawn on the test background" if card else "the card came back empty",
                                "" if card else "check the bot logs"))
        except Exception as e:
            card = None
            checks.append(Check("Welcome card", False, f"drawing the card failed ({type(e).__name__})", "check the bot logs"))

    try:
        await posted.delete()
    except Exception:
        checks.append(Check("Cleanup", None, "the test message could not be deleted (harmless)", f"delete it by hand in #{channel.name}"))
    return checks, card


def format_report(checks: list, bot_label: str = "") -> str:
    bad = [c for c in checks if c.ok is False]
    head = ("❌ **Image hosting has a problem**" if bad else "✅ **Image hosting works**") + (f" ({bot_label})" if bot_label else "")
    lines = [head]
    for c in checks:
        icon = "✅" if c.ok else ("❌" if c.ok is False else "ℹ️")
        lines.append(f"{icon} **{c.name}**: {c.detail}")
        if c.ok is False and c.fix:
            lines.append(f"   ↳ Fix: {c.fix}")
    if not bad:
        lines.append("-# Custom welcome backgrounds will be sent for servers that have one.")
    return "\n".join(lines)[:1900]


def problems(checks: list) -> list:
    return [c for c in checks if c.ok is False]


def bot_label(bot) -> str:
    cid = getattr(bot, "clone_id", None)
    return "main bot" if cid is None else f"clone #{cid}"


async def notify_owners(bot, key: str, text: str, cooldown: int = ALERT_COOLDOWN_SECONDS) -> bool:
    """DM the bot owners about a problem, at most once per `cooldown` seconds per (bot, key). Returns True when sent."""
    import config
    k = (getattr(bot, "clone_id", None), key)
    now = time.monotonic()
    if now - _alert_sent.get(k, -1e12) < cooldown:
        return False
    _alert_sent[k] = now
    sent = False
    for uid in sorted(getattr(config, "DISCORD_OWNER_BROADCAST_IDS", None) or ()):
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(f"⚠️ [{bot_label(bot)}] {text}"[:1900])
            sent = True
        except Exception:
            logger.warning("image host: couldn't DM owner %s", uid)
    return sent


async def startup_check(bot, delay: float = 20.0) -> None:
    """Once per process, a quiet check (no test upload). Only a problem is reported: logged, and sent to the owners."""
    global _startup_done
    if _startup_done:
        return
    _startup_done = True
    await asyncio.sleep(delay)
    try:
        checks, _ = await diagnose(bot, post_test=False)
        bad = problems(checks)
        if bad:
            report = format_report(checks, bot_label(bot))
            logger.warning("[image-host] startup check found a problem: %s", "; ".join(f"{c.name}: {c.detail}" for c in bad))
            await notify_owners(bot, "startup", report + "\nCustom welcome backgrounds will not be sent until this is fixed.")
        else:
            logger.info("[image-host] startup check ok (%s)", bot_label(bot))
    except Exception:
        logger.warning("[image-host] startup check crashed", exc_info=True)


async def report_render_problem(bot, guild_id, reason: str) -> None:
    """A server HAS a custom background but this join was drawn with the stock card. Tell the owners (rate limited)."""
    logger.warning("[image-host] custom background not used for guild %s: %s", guild_id, reason)
    await notify_owners(bot, f"render:{guild_id}",
                        f"A custom welcome background was NOT used for server `{guild_id}`: {reason}. "
                        "Run **Test image hosting** in the owner panel (System). The server admin may need to run `/welcome custombg` again.")


# ── moving saved backgrounds into the shared channel ─────────────────────────────────────────────────────────
@dataclass
class MoveResult:
    checked: int = 0
    already_ok: int = 0
    moved: int = 0
    waiting: int = 0                                  # left for the next run (per-run limit)
    failed: list = None                               # [(guild_id, reason)]
    error: str = ""                                   # the whole run could not start

    def __post_init__(self):
        if self.failed is None:
            self.failed = []


def _sniff_ext(data: bytes) -> str:
    return "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"


async def post_to_host(bot, data: bytes, label: str):
    """Re-post image bytes to the hosting channel THIS bot can reach. Returns (channel_id, message_id, cdn_url) or None."""
    channel = await resolve_channel(bot)
    if channel is None:
        return None
    try:
        posted = await channel.send(content=label[:200], file=discord.File(io.BytesIO(data), filename=f"background.{_sniff_ext(data)}"))
    except discord.HTTPException:
        logger.warning("image host: couldn't post a background to the hosting channel", exc_info=True)
        return None
    return channel.id, posted.id, (posted.attachments[0].url if posted.attachments else None)


async def _saved_bytes(bot, row: dict):
    """(bytes, None) or (None, reason): the saved background, from its hosting message first, then from the stored link.
    Same strict checks as the welcome card (png/jpeg, size cap)."""
    from discord_bot.cogs import welcome as welcome_cog
    reasons = []
    url = None
    if row.get("custom_bg_channel_id") and row.get("custom_bg_message_id"):
        url = await welcome_cog._refresh_custom_bg_url(bot, row)
        if url:
            data, why = await _fetch_bytes(url)
            if data:
                return data, None
            reasons.append(why)
        else:
            reasons.append("its hosting message is gone or unreachable")
    stored = str(row.get("custom_background_url") or "").strip()
    if stored and stored != url:
        data, why = await _fetch_bytes(stored)
        if data:
            return data, None
        reasons.append(why)
    return None, "; ".join(r for r in reasons if r) or "no saved image to read"


async def move_backgrounds(bot, *, limit: int = MOVE_PER_RUN, pause: float = MOVE_PAUSE_SECONDS) -> MoveResult:
    """Make sure every server of THIS bot that has a saved custom background keeps it in the shared hosting channel.
    Servers already there are left alone; the others are re-posted there and pointed at the new message. A server whose
    image can no longer be read is reported (never silently dropped, and its old settings are not touched)."""
    from database import db
    out = MoveResult()
    target = await resolve_channel(bot)
    if target is None:
        out.error = "no hosting channel this bot can reach"
        return out
    clone = getattr(bot, "clone_id", None)
    rows = await db.welcome_custom_bg_rows(clone)
    out.checked = len(rows)
    todo = []
    for r in rows:
        if r.get("custom_bg_channel_id") == target.id and r.get("custom_bg_message_id"):
            out.already_ok += 1
        else:
            todo.append(r)
    out.waiting = max(0, len(todo) - limit)
    for r in todo[:limit]:
        gid = r["guild_id"]
        try:
            data, why = await _saved_bytes(bot, r)
            if data is None:
                out.failed.append((gid, why))
                continue
            posted = await post_to_host(bot, data, f"Custom welcome background (moved) — guild `{gid}`")
            if posted is None:
                out.failed.append((gid, "the hosting channel refused the upload"))
                continue
            ch_id, msg_id, cdn = posted
            changed = await db.welcome_custom_bg_move(gid, clone, r.get("custom_bg_message_id"), r.get("custom_background_url"),
                                                      ch_id, msg_id, cdn or r.get("custom_background_url"))
            if changed:
                out.moved += 1
            else:
                out.already_ok += 1                                           # the admin saved a new one meanwhile: leave it
            await asyncio.sleep(pause)
        except Exception as e:
            logger.warning("image host: moving the background of guild %s failed", gid, exc_info=True)
            out.failed.append((gid, f"unexpected error ({type(e).__name__})"))
    return out


def format_move_report(res: MoveResult, label: str = "") -> str:
    head = f"🖼️ **Saved backgrounds**" + (f" ({label})" if label else "")
    if res.error:
        return f"{head}\n❌ Nothing was moved: {res.error}. Press **Test image hosting** to see why."
    lines = [head,
             f"✅ Already in the hosting channel: **{res.already_ok}**   ➡️ Moved now: **{res.moved}**   ❌ Could not move: **{len(res.failed)}**"
             + (f"   ⏳ Waiting for the next run: **{res.waiting}**" if res.waiting else "")]
    for gid, why in res.failed[:10]:
        lines.append(f"❌ Server `{gid}`: {why}")
    if len(res.failed) > 10:
        lines.append(f"…and {len(res.failed) - 10} more (see the bot log).")
    if res.failed:
        lines.append("-# Those servers keep the stock card until their admin runs `/welcome custombg` and uploads the image again.")
    return "\n".join(lines)[:1900]


async def _tell_server_owners(bot, failed: list) -> int:
    """DM the owner of each server whose background can't be moved, at most once a week per server."""
    if not NOTIFY_SERVER_OWNERS or not failed:
        return 0
    from datetime import datetime, timezone, timedelta
    from database import db
    sent = 0
    now = datetime.now(timezone.utc)
    for gid, why in failed:
        if sent >= MAX_SERVER_NOTICES_PER_RUN:
            break
        key = f"bgmove_notified:{getattr(bot, 'clone_id', None)}:{gid}"
        try:
            last = await db.get_global_setting(key)
            if last and now - datetime.fromisoformat(last) < timedelta(days=SERVER_NOTICE_COOLDOWN_DAYS):
                continue
            guild = bot.get_guild(int(gid))
            if guild is None or not guild.owner_id:
                continue
            owner = guild.owner or await bot.fetch_user(guild.owner_id)
            await owner.send(f"Hi! The custom welcome background for **{guild.name}** can't be loaded any more ({why}), so new members "
                             "get the standard card. Run `/welcome custombg` in your server and upload the image again to fix it.")
            await db.set_global_setting(key, now.isoformat())
            sent += 1
        except Exception:
            logger.warning("image host: couldn't tell the owner of guild %s", gid, exc_info=True)
    return sent


async def auto_move(bot) -> MoveResult:
    """One scheduled run: move what can be moved, tell the bot owners and the server owners about what can't."""
    res = await move_backgrounds(bot)
    logger.info("[image-host] background move (%s): checked=%s ok=%s moved=%s failed=%s waiting=%s %s", bot_label(bot), res.checked,
                res.already_ok, res.moved, len(res.failed), res.waiting, res.error)
    if res.error:
        await notify_owners(bot, "move-error", format_move_report(res, bot_label(bot)), cooldown=24 * 3600)
    elif res.failed:
        await notify_owners(bot, "move-failed", format_move_report(res, bot_label(bot)), cooldown=24 * 3600)
        await _tell_server_owners(bot, res.failed)
    return res


async def background_jobs(bot, delay: float = 25.0) -> None:
    """Started once per process by the welcome cog: the quiet startup check, then the background mover every 12 hours."""
    global _jobs_started
    if _jobs_started:
        return
    _jobs_started = True
    await startup_check(bot, delay=delay)
    while True:
        try:
            await auto_move(bot)
        except Exception:
            logger.warning("[image-host] background move crashed", exc_info=True)
        await asyncio.sleep(MOVE_EVERY_SECONDS)
