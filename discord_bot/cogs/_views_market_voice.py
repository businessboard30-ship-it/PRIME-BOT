# path: discord_bot/cogs/_views_market_voice.py

"""
Market voice messages, Discord side (R2b). Rules and storage live in modules/market_voice.py.

Flow (play coins only, voice only, no text relayed):
  1. Seller turns voice ON (Voice button in the Market screen). Default is OFF.
  2. Buyer opens a listing and taps "Voice message". The bot waits 3 minutes for ONE voice message from that buyer in a DM.
  3. The bot reads the clip, stores it (audio only while pending), tries to delete the buyer's original and DMs the seller a
     card with Play / Report / Block. A bot cannot delete a user's message in a DM, so the buyer is told to delete it.
  4. Seller taps Play: the audio leaves the database, is sent once, and that message is deleted by the sweeper after 2 minutes.
  5. Sweeper (main bot): deletes played messages, expires unplayed ones (24 h), clears messages of closed listings.
Never logs audio. Unplayed messages can also be played from the Voice screen if the seller's DMs are closed.
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Dict, Optional, Tuple

import discord
from discord_bot.cogs import _cat_visibility as vis

from modules import cat_requests
from modules import market_voice as mv
from modules import ops_reports

logger = logging.getLogger(__name__)

WAIT_SECONDS = 180
_waiting: Dict[int, Tuple[int, float]] = {}        # buyer id -> (listing id, deadline). Per process, short lived, no audio.

_SEND_TEXT = {
    "bad": "That has to be a Discord voice message (the mic button).", "big": "That clip is too large.",
    "long": f"Voice messages can be up to {mv.MAX_SECONDS} seconds.", "no_listing": "That listing is not open any more.",
    "own": "That is your own listing.", "off": "This seller is not taking voice messages.",
    "blocked": "You can't send a voice message to this seller.",
    "limit_listing": f"You can send {mv.PER_LISTING} voice messages per listing.",
    "limit_day": f"You reached today's limit of {mv.PER_DAY} voice messages.",
    "limit_seller": "This seller has too many unplayed messages. Try again later.",
}


def start_wait(user_id: int, listing_id: int) -> None:
    _waiting[int(user_id)] = (int(listing_id), time.monotonic() + WAIT_SECONDS)


def _take_wait(user_id: int) -> Optional[int]:
    got = _waiting.pop(int(user_id), None)
    if got is None or got[1] < time.monotonic():
        return None
    return got[0]


def voice_attachment(message: discord.Message) -> Optional[discord.Attachment]:
    for a in message.attachments:
        if a.is_voice_message():
            return a
    return None


async def handle_incoming(client, message: discord.Message) -> bool:
    """Called for every DM. Returns True when the message was a voice message we were waiting for."""
    if message.author.bot or message.guild is not None:
        return False
    att = voice_attachment(message)
    if att is None:
        return False
    listing_id = _take_wait(message.author.id)
    if listing_id is None:
        return False
    dur_ms = int(round(float(att.duration or 0) * 1000))
    if att.size > mv.MAX_BYTES:
        await message.channel.send(_SEND_TEXT["big"])
        return True
    try:
        audio = await att.read()
    except Exception:
        logger.warning("swallowed exception in handle_incoming", exc_info=True)
        await message.channel.send("I couldn't read that clip. Tap Voice message again to retry.")
        return True
    res = await mv.send(message.author.id, listing_id, audio, dur_ms)
    del audio
    if res.status != "ok":
        await message.channel.send(_SEND_TEXT.get(res.status, "That didn't work."))
        return True
    deleted = False
    try:
        await message.delete()
        deleted = True
    except Exception:
        logger.warning("swallowed exception in handle_incoming", exc_info=True)
        pass
    note = "" if deleted else "\nDiscord does not let a bot delete your message in a DM, so delete your own recording if you like."
    delivered = await _deliver_card(client, res.voice_id, res.receiver_id)
    where = "sent to the seller" if delivered else "waiting for the seller in their Voice screen"
    await message.channel.send(f"\U0001F399\uFE0F Your voice message is {where}. It is removed from the bot once played or after "
                               f"{mv.EXPIRY_HOURS} hours.{note}")
    return True


def card_layout(voice_id: int, sender_id: int, duration_ms: int) -> discord.ui.LayoutView:
    v = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=discord.Color.gold())
    box.add_item(discord.ui.TextDisplay(f"## \U0001F399\uFE0F Voice message about your listing\nFrom <@{sender_id}> \u00b7 {duration_ms / 1000:.0f}s\n"
                                        f"-# Tap Play to hear it. {mv.PRIVACY}"))
    box.add_item(discord.ui.ActionRow(PlayButton(voice_id), ReportButton(voice_id), BlockButton(voice_id, sender_id)))
    v.add_item(box)
    return v


async def _deliver_card(client, voice_id: int, receiver_id: int) -> bool:
    try:
        user = await client.fetch_user(receiver_id)
        msg = await user.send(view=card_layout(voice_id, await _sender_of(voice_id), await _duration_of(voice_id)),
                              allowed_mentions=discord.AllowedMentions.none())
        await mv.set_card(voice_id, msg.channel.id, msg.id)
        return True
    except Exception:
        logger.debug("[market-voice] couldn't DM the seller", exc_info=True)
        return False


async def _row(voice_id: int):
    from database import get_pool
    p = await get_pool()
    async with p.acquire() as conn:
        return await conn.fetchrow("SELECT sender_id, duration_ms FROM market_voice WHERE id = $1", int(voice_id))


async def _sender_of(voice_id: int) -> int:
    r = await _row(voice_id)
    return int(r["sender_id"]) if r else 0


async def _duration_of(voice_id: int) -> int:
    r = await _row(voice_id)
    return int(r["duration_ms"]) if r else 0


async def play(interaction: discord.Interaction, voice_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    res = await mv.claim_play(voice_id, interaction.user.id)
    if res.status != "ok":
        text = {"late": "That voice message expired.", "not_yours": "That message is not for you."}.get(res.status, "That voice message is gone.")
        await interaction.followup.send(text, ephemeral=True)
        return
    try:
        msg = await interaction.followup.send(file=discord.File(io.BytesIO(res.audio), filename="voice-message.ogg"), ephemeral=False
                                              if interaction.guild is None else True, wait=True)
    except Exception:
        logger.warning("[market-voice] sending the played audio failed", exc_info=True)
        await mv.reopen(voice_id, res.audio)
        await interaction.followup.send("I couldn't send it. Tap Play again.", ephemeral=True)
        return
    finally:
        res = None
    await mv.set_played(voice_id, msg.channel.id, msg.id)
    await interaction.followup.send(f"It is removed in about {mv.PLAY_KEEP_SECONDS // 60} minutes.", ephemeral=True)


class PlayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^market_voice:play:(\d+)$"):
    def __init__(self, voice_id: int):
        self.voice_id = voice_id
        super().__init__(discord.ui.Button(label="Play", emoji="\u25B6\uFE0F", style=discord.ButtonStyle.success,
                                           custom_id=f"market_voice:play:{voice_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await play(interaction, self.voice_id)


class ReportButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^market_voice:report:(\d+)$"):
    def __init__(self, voice_id: int):
        self.voice_id = voice_id
        super().__init__(discord.ui.Button(label="Report", style=discord.ButtonStyle.secondary, custom_id=f"market_voice:report:{voice_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        r = await mv.report(self.voice_id, interaction.user.id)
        if r == "ok":                                     # metadata only: ids, never audio
            await ops_reports.submit(interaction.client, f"voice-report-{self.voice_id}",
                                     f"Market voice report: message #{self.voice_id}, reported by {interaction.user.id}. "
                                     "Details are in market_reports (the audio is not kept).")
        text = {"ok": "Reported. The owner can review it.", "again": "You already reported this one."}.get(r, "I can't report that one.")
        await interaction.response.send_message(text, ephemeral=True)


class BlockButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^market_voice:block:(\d+):(\d+)$"):
    def __init__(self, voice_id: int, sender_id: int):
        self.voice_id, self.sender_id = voice_id, sender_id
        super().__init__(discord.ui.Button(label="Block", style=discord.ButtonStyle.danger,
                                           custom_id=f"market_voice:block:{voice_id}:{sender_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)), int(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        if not await cat_requests.block(interaction.user.id, self.sender_id):
            await interaction.response.send_message("That didn't work.", ephemeral=True)
            return
        gone = await mv.purge_pair(interaction.user.id, self.sender_id)
        await delete_messages(interaction.client, [(c, m) for _, c, m in gone])
        await interaction.response.send_message("Blocked. Their voice messages to you are removed.", ephemeral=True)


async def delete_messages(client, pairs) -> None:
    for channel_id, message_id in pairs:
        if not (channel_id and message_id):
            continue
        try:
            await client.get_partial_messageable(int(channel_id)).get_partial_message(int(message_id)).delete()
        except Exception:
            logger.debug("[market-voice] couldn't delete a message", exc_info=True)


# ── Voice screen (opened from the Market screen) ─────────────────────────

class VoiceView(discord.ui.LayoutView):
    def __init__(self, user, on: bool, pending):
        super().__init__(timeout=300)
        self.user = user
        box = discord.ui.Container(accent_colour=discord.Color.gold())
        lines = [f"## \U0001F399\uFE0F Voice messages\nVoice from buyers is **{'ON' if on else 'OFF'}**. {mv.PRIVACY}"]
        for p in pending:
            lines.append(f"\u2022 #{p.id} from <@{p.sender_id}> \u00b7 {p.duration_ms / 1000:.0f}s \u00b7 listing #{p.listing_id}")
        if not pending:
            lines.append("No unplayed messages.")
        box.add_item(discord.ui.TextDisplay("\n".join(lines)[:3500]))
        t = discord.ui.Button(label="Turn off" if on else "Turn on", style=discord.ButtonStyle.primary)
        t.callback = lambda i: self._toggle(i, not on)
        row = [t]
        if pending:
            sel = discord.ui.Select(placeholder="Play one\u2026", options=[
                discord.SelectOption(label=f"#{p.id} \u00b7 {p.duration_ms / 1000:.0f}s", value=str(p.id)) for p in pending[:10]])
            sel.callback = lambda i: play(i, int(i.data["values"][0]))
            box.add_item(discord.ui.ActionRow(sel))
        box.add_item(discord.ui.ActionRow(*row))
        self.add_item(box)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Open your own Voice screen from the Market.", ephemeral=True)
            return False
        return True

    async def _toggle(self, interaction, on: bool):
        await mv.set_voice_on(interaction.user.id, on)
        await open_screen(interaction, edit=True)


async def open_screen(interaction: discord.Interaction, edit: bool = False) -> None:
    on = await mv.voice_on(interaction.user.id)
    view = VoiceView(interaction.user, on, await mv.pending_for(interaction.user.id))
    if edit:
        await vis.edit(interaction, view=view)
    else:
        await vis.send(interaction, view=view)


# ── sweeper (main bot) ───────────────────────────────────────────────────

async def sweep(client) -> None:
    due = await mv.due_to_delete()
    await delete_messages(client, [(c, m) for _, c, m in due])
    for vid, _, _ in due:
        await mv.mark_deleted(vid)
    gone = (await mv.expire_due()) + (await mv.purge_closed_listings())
    await delete_messages(client, [(c, m) for _, c, m in gone])
    await mv.prune()


DYNAMIC_ITEMS = (PlayButton, ReportButton, BlockButton)
