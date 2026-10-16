# path: discord_bot/cogs/_views_scam_submit.py

"""
Scam Shield user submissions.

The "scam caught" notice has a button. Anyone can press it, upload scam images and/or describe the scam, and it is
posted to Scam Shield's storage (evidence) channel with Approve and Deny buttons. Only bot owners can press those.

  * Approve: every image on the submission is hashed into a Scam Shield image rule (live in every server within
    seconds). Text-only submissions are marked approved but add no rule by themselves: the owner adds the word or
    domain from /admin -> Scam Shield, so nobody can slip a rule in through free text.
  * Deny: marks it denied. The files stay in the storage channel.

The review view is persistent (fixed custom_ids) and keeps no state: the submitter id sits in the embed footer and
the pictures are the message's own attachments, so Approve/Deny keep working after a restart.
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import List, Optional, Tuple

import discord

from modules import scam_shield as ss

logger = logging.getLogger(__name__)

_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")
_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 4
MAX_TEXT = 1000
COOLDOWN_SECONDS = 120
MODAL_TIMEOUT = 600
_FOOTER = re.compile(r"submitted by (\d+)")

_last: dict = {}                  # user id -> monotonic time of their last submission


def _is_image(name: str, content_type: Optional[str]) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in _TYPES or (not ct and (name or "").lower().endswith(_EXTS))


def _owner_ids() -> frozenset:
    try:
        from config import DISCORD_OWNER_BROADCAST_IDS
        return frozenset(int(i) for i in DISCORD_OWNER_BROADCAST_IDS)
    except Exception:
        return frozenset()


async def _storage_channel(bot) -> Optional[discord.TextChannel]:
    """Scam Shield's evidence channel (/admin scamchannel), else the image-hosting channel."""
    cid = ss.evidence_channel_id()
    if cid:
        ch = bot.get_channel(cid)
        if ch is None:
            try:
                ch = await bot.fetch_channel(cid)
            except discord.HTTPException:
                ch = None
        if isinstance(ch, discord.TextChannel):
            return ch
    from discord_bot.ad_images import _host_channel
    return await _host_channel(bot)


class ReviewView(discord.ui.View):
    """Approve / Deny under a submission in the storage channel. Owner only. Persistent."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in _owner_ids():
            await interaction.response.send_message("Only the bot owners can review scam submissions.", ephemeral=True)
            return False
        return True

    @staticmethod
    def _pending(interaction: discord.Interaction) -> Optional[discord.Embed]:
        msg = interaction.message
        if msg is None or not msg.embeds:
            return None
        return msg.embeds[0]

    async def _finish(self, interaction: discord.Interaction, embed: discord.Embed, verdict: str, color, extra: str = ""):
        embed = embed.copy()
        embed.color = color
        embed.add_field(name="Review", value=f"{verdict} by <@{interaction.user.id}>{extra}"[:1000], inline=False)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, custom_id="ssub:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self._pending(interaction)
        if embed is None:
            await interaction.response.send_message("I can't read this submission.", ephemeral=True)
            return
        await interaction.response.defer()          # hashing + database can take a moment
        added = dup = bad = 0
        for att in (interaction.message.attachments or [])[:MAX_FILES]:
            if not _is_image(att.filename, att.content_type) or att.size > MAX_BYTES:
                continue
            try:
                h = ss.dhash(await att.read())
            except discord.HTTPException:
                h = None
            if h is None:
                bad += 1
                continue
            try:
                rid = await ss.add_rule("image", format(h, "016x"), interaction.user.id,
                                        f"user submission {interaction.message.id}")
            except Exception:
                logger.exception("[scam-submit] couldn't add the approved image rule")
                await interaction.followup.send("Database problem. Nothing was changed, try again.", ephemeral=True)
                return
            added += 1 if rid else 0
            dup += 0 if rid else 1
        extra = f" · {added} image rule(s) added" + (f", {dup} already existed" if dup else "") + \
            (f", {bad} unreadable" if bad else "")
        embed = embed.copy()
        embed.color = discord.Color.green()
        embed.add_field(name="Review", value=f"✅ Approved by <@{interaction.user.id}>{extra}"[:1000], inline=False)
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Deny", emoji="🚫", style=discord.ButtonStyle.danger, custom_id="ssub:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self._pending(interaction)
        if embed is None:
            await interaction.response.send_message("I can't read this submission.", ephemeral=True)
            return
        await self._finish(interaction, embed, "🚫 Denied", discord.Color.dark_grey())


class SubmitScamModal(discord.ui.Modal, title="Submit a scam"):
    def __init__(self):
        super().__init__(timeout=MODAL_TIMEOUT)
        self.files = discord.ui.FileUpload(required=False, min_values=0, max_values=MAX_FILES)
        self.add_item(discord.ui.Label(
            text="Upload the scam (screenshot or image)",
            description=f"PNG, JPG, WEBP or GIF, up to {MAX_BYTES // (1024 * 1024)}MB each, {MAX_FILES} at most",
            component=self.files))
        self.details = discord.ui.TextInput(label="What is it? (optional)", style=discord.TextStyle.paragraph,
                                            max_length=MAX_TEXT, required=False,
                                            placeholder="A link, a word, a message, how it tricks people...")
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        uploads = list(self.files.values or [])
        text = (self.details.value or "").strip()
        if not uploads and not text:
            await interaction.response.send_message("Nothing to send. Upload a picture or describe the scam.", ephemeral=True)
            return
        now = time.monotonic()
        if now - _last.get(interaction.user.id, -1e9) < COOLDOWN_SECONDS:
            await interaction.response.send_message("You just sent one. Please wait a couple of minutes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        files: List[Tuple[str, bytes]] = []
        skipped = 0
        for att in uploads[:MAX_FILES]:
            if att.size > MAX_BYTES or not _is_image(att.filename, att.content_type):
                skipped += 1
                continue
            try:
                files.append((att.filename, await att.read()))
            except discord.HTTPException:
                skipped += 1
        if uploads and not files and not text:
            await interaction.followup.send("⚠️ Use a PNG, JPG, WEBP or GIF under 8MB.", ephemeral=True)
            return
        host = await _storage_channel(interaction.client)
        if host is None:
            await interaction.followup.send("Sorry, I can't take submissions right now (no storage channel is set).", ephemeral=True)
            return
        embed = discord.Embed(title="🛡️ Scam Shield submission", color=discord.Color.orange(),
                              description=discord.utils.escape_mentions(text)[:MAX_TEXT] or "(picture only)")
        embed.add_field(name="From", value=f"{discord.utils.escape_markdown(str(interaction.user))[:40]} (`{interaction.user.id}`)")
        if interaction.guild:
            embed.add_field(name="Server", value=f"`{interaction.guild.id}`")
        embed.add_field(name="Pictures", value=str(len(files)))
        embed.set_footer(text=f"submitted by {interaction.user.id}")
        try:
            await host.send(embed=embed, view=ReviewView(),
                            files=[discord.File(io.BytesIO(d), filename=f"submission_{i}_{n}"[:100]) for i, (n, d) in enumerate(files)],
                            allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.warning("[scam-submit] couldn't post the submission", exc_info=True)
            await interaction.followup.send("Sorry, I couldn't send that. Please try again later.", ephemeral=True)
            return
        _last[interaction.user.id] = now
        tail = f" ({skipped} file(s) skipped: wrong type or over 8MB.)" if skipped else ""
        await interaction.followup.send("✅ Thank you! Your scam report was sent for review." + tail, ephemeral=True)


class SubmitScamView(discord.ui.View):
    """The button under the in-channel \"scam caught\" notice. Anyone can press it."""

    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        btn = discord.ui.Button(label="Submit a scam", emoji="📤", style=discord.ButtonStyle.secondary)
        btn.callback = self._open
        self.add_item(btn)

    async def _open(self, interaction: discord.Interaction):
        if interaction.user.bot:
            return
        await interaction.response.send_modal(SubmitScamModal())
