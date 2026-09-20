"""
/botprofile -- Premium-only custom branding.

A paying server can give the bot its own name, avatar and banner *in that
server only* (Discord's per-server bot profile). Other servers keep seeing
the normal bot. Only server admins (Manage Server) can use it.

discord.py 2.6 can't set a per-server avatar/banner, so this calls Discord's
"Modify Current Member" endpoint (PATCH /guilds/{id}/members/@me) directly.
"""

import asyncio
import base64
import io
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from discord.http import Route

from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 25_000_000
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
COOLDOWN_SECONDS = 30

# Names that would let a server pass the bot off as Discord itself, or ping
# people through the name. Deliberately small: a server naming its own bot
# "Staff Bot" is fine, impersonating Discord is not.
_BLOCKED_WORDS = ("discord", "clyde", "everyone")     # matched even with spaces/symbols between letters
_BLOCKED_LITERALS = ("http", ".gg/", "://")           # matched as written
_BLOCKED_EXACT = {"here"}

_last_change: dict = {}   # guild_id -> monotonic time of last successful edit


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _name_problem(name: str):
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= 32:
        return "The name must be 1-32 characters."
    folded = re.sub(r"\s+", " ", cleaned).casefold()
    squashed = re.sub(r"[^a-z0-9]", "", folded)
    if folded in _BLOCKED_EXACT:
        return "That name isn't allowed."
    if any(w in squashed for w in _BLOCKED_WORDS) or any(l in folded for l in _BLOCKED_LITERALS):
        return "That name could be mistaken for Discord or contain a link, so it isn't allowed."
    if "@" in cleaned:
        return "The name can't contain @."
    return None


def _process_image(data: bytes, kind: str):
    """Runs in a worker thread. Returns (bytes, mime) or raises ValueError."""
    from PIL import Image
    try:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("I couldn't read that image. Use a normal png, jpeg or webp file.")
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("That image is too large.")

    if kind == "avatar":
        side = min(img.width, img.height)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize((512, 512))
        out = io.BytesIO()
        img.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"

    # banner: crop to 16:9, then 960x540
    target = 16 / 9
    w, h = img.size
    if w / h > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((960, 540))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue(), "image/jpeg"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class BotProfileCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.guild_only()(app_commands.Group(
        name="botprofile",
        description="Premium: give the bot a custom name, avatar and banner in this server",
    ))

    # ── shared gate ──────────────────────────────────────────────────────
    async def _gate(self, interaction: discord.Interaction, require_premium: bool = True) -> bool:
        """Defers, then checks Manage Server + Premium + cooldown.
        Returns True when the caller may proceed. Reset skips the Premium
        check so a server whose Premium lapsed can still undo its branding."""
        await interaction.response.defer(ephemeral=True)
        if not interaction.permissions.manage_guild:
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return False
        clone_id = _clone_id_of(interaction)
        try:
            premium = bool(await db.is_guild_premium_active(interaction.guild_id, clone_id))
        except Exception:
            premium = False
        if require_premium and not premium:
            from discord_bot.cogs._views_premium import send_premium_pitch
            await interaction.followup.send(
                "🎨 **Custom bot branding is a Premium feature.** Upgrade this server to give the bot your own name, avatar and banner.",
                ephemeral=True,
            )
            await send_premium_pitch(interaction, interaction.guild_id, clone_id)
            return False
        wait = COOLDOWN_SECONDS - (time.monotonic() - _last_change.get(interaction.guild_id, 0))
        if wait > 0:
            await interaction.followup.send(f"Slow down a little. Try again in {int(wait) + 1} seconds.", ephemeral=True)
            return False
        return True

    async def _apply(self, interaction: discord.Interaction, payload: dict, ok_message: str):
        route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=interaction.guild_id)
        try:
            await self.bot.http.request(route, json=payload, reason=f"Custom bot profile set by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(
                "I'm not allowed to do that here. Give me the **Change Nickname** permission and try again.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            logger.warning("botprofile: edit failed in guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send(
                "Discord rejected that change (it may be rate-limited, or the image was refused). Wait a bit and try again.",
                ephemeral=True,
            )
            return
        _last_change[interaction.guild_id] = time.monotonic()
        logger.info("botprofile: guild=%s user=%s changed %s", interaction.guild_id, interaction.user.id, sorted(payload))
        await interaction.followup.send(ok_message, ephemeral=True)

    async def _read_image(self, interaction: discord.Interaction, attachment: discord.Attachment, kind: str):
        ctype = (attachment.content_type or "").split(";")[0].strip().lower()
        if ctype not in ALLOWED_TYPES:
            await interaction.followup.send("Please upload a png, jpeg or webp image.", ephemeral=True)
            return None
        if attachment.size > MAX_UPLOAD_BYTES:
            await interaction.followup.send("That file is too big. Keep it under 4 MB.", ephemeral=True)
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.read()
            processed, mime = await asyncio.to_thread(_process_image, data, kind)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return None
        except Exception:
            await interaction.followup.send("I couldn't download that image. Try uploading it again.", ephemeral=True)
            return None
        return _data_uri(processed, mime)

    # ── commands ─────────────────────────────────────────────────────────
    @group.command(name="name", description="Premium: set the bot's name in this server")
    @app_commands.describe(name="The name to show for the bot here (1-32 characters)")
    async def name_cmd(self, interaction: discord.Interaction, name: str):
        if not await self._gate(interaction):
            return
        problem = _name_problem(name)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        await self._apply(interaction, {"nick": name.strip()}, f"✅ The bot is now called **{name.strip()}** in this server.")

    @group.command(name="avatar", description="Premium: set the bot's avatar in this server")
    @app_commands.describe(image="A png, jpeg or webp picture (it's cropped to a square)")
    async def avatar_cmd(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._gate(interaction):
            return
        uri = await self._read_image(interaction, image, "avatar")
        if uri is None:
            return
        await self._apply(interaction, {"avatar": uri}, "✅ The bot has a new avatar in this server. It can take a moment to show up.")

    @group.command(name="banner", description="Premium: set the bot's profile banner in this server")
    @app_commands.describe(image="A png, jpeg or webp picture (it's cropped to widescreen)")
    async def banner_cmd(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._gate(interaction):
            return
        uri = await self._read_image(interaction, image, "banner")
        if uri is None:
            return
        await self._apply(interaction, {"banner": uri}, "✅ The bot has a new banner in this server. It can take a moment to show up.")

    @group.command(name="reset", description="Put the bot's name, avatar and banner back to normal in this server")
    async def reset_cmd(self, interaction: discord.Interaction):
        if not await self._gate(interaction, require_premium=False):
            return
        await self._apply(
            interaction, {"nick": None, "avatar": None, "banner": None},
            "✅ The bot's name, avatar and banner are back to normal in this server.",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BotProfileCog(bot))
