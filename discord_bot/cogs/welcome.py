# path: discord_bot/cogs/welcome.py

"""
Welcome cards — Discord equivalent of ProBot's join messages. On
on_member_join, if enabled for the guild, fetches the new member's avatar
and renders it into a card image (modules/welcome_card.py) posted alongside
a templated text message to a configured channel.

Deliberately separate from discord_bot/cogs/automod.py's on_member_join
(the min-account-age raid gate) — a raid kick and a welcome card are
unrelated concerns that happen to share an event, and automod firing first
(kicking the member) naturally means this listener's guild.get_member(...)
lookups still work fine either order since discord.py fires all listeners
for the same event.
"""

import logging
import io
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog

import config as bot_config
from database import db
from modules.welcome_card import render_welcome_card
from discord_bot.cogs._views_shared import refresh_button
from discord_bot.cogs._views_welcome import build_wizard_view, refresh_posted_wizard

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_NAME_HINTS = ("welcome", "general", "lobby", "entrance")


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    """Checks the invoking user's permission in the current channel.

    Uses interaction.permissions (always populated by Discord for any
    command run inside a guild channel) rather than
    interaction.user.guild_permissions, because interaction.user comes
    back as a plain discord.User instead of discord.Member when this app
    is invoked via a user-install context — even while run inside a real
    server channel — which made guild_permissions unreachable for anyone
    using the bot as a personal (user-installed) app, including owners.
    """
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


async def _deny(interaction: discord.Interaction, perm_name: str):
    msg = f"You need the **{perm_name}** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as premium.py/leveling.py: None on the main bot, the
    clone's row id on a clone process — keeps welcome config separate per
    clone in a guild more than one process is in."""
    return getattr(interaction.client, "clone_id", None)


def _apply_template(template: str, member: discord.Member) -> str:
    return (
        template
        .replace("{member}", member.mention)
        .replace("{guild}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


def _suggested_template(guild: discord.Guild) -> str:
    """Builds a one-off suggested welcome line for the nudge DM. Uses the
    server's own description (Community servers can set one) so the
    suggestion sounds like it belongs to that server instead of a generic
    default — falls back to the plain default template if the guild has
    no description set."""
    if guild.description:
        desc = guild.description.strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        return f"Welcome {{member}} to {{guild}}! {desc} You're member #{{count}}."
    return "Welcome {member} to {guild}! You are member #{count}."


async def _fetch_sticker_bytes(session: aiohttp.ClientSession, sticker_url: str | None) -> bytes | None:
    """Downloads the configured sticker image, if any. Requires a direct
    link to the image file itself (e.g. a media.tenor.com/....gif URL, or
    a link ending in .gif/.webp/.png) — a tenor.com/view/... page URL is
    an HTML page, not an image, and won't decode. On any failure this
    just returns None so the card still renders (with an empty sticker
    spot) instead of failing the whole join event."""
    sticker_url = (sticker_url or "").strip()
    if not sticker_url:
        return None
    try:
        async with session.get(sticker_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception as e:
        logger.warning(f"[v0] Couldn't fetch sticker from {sticker_url}: {e}")
        return None


# Ultra-pack custom backgrounds: a much stricter fetch than
# _fetch_sticker_bytes above, since this one is a SET-time admin action
# (must give a clear pass/fail reason) rather than a best-effort render-time
# lookup. Only real image files, capped well under Discord's own upload
# limits so a render never has to decode something huge on every join.
CUSTOM_BG_ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/jpg")
CUSTOM_BG_MAX_BYTES = 8 * 1024 * 1024  # 8 MB


async def _fetch_custom_bg_bytes(
    session: aiohttp.ClientSession, url: str
) -> tuple[bytes | None, str | None]:
    """Downloads and validates a candidate ultra-pack background. Returns
    (bytes, None) on success or (None, reason) on failure — the reason is
    meant to be shown straight to the admin who ran /welcome custombg, so
    it stays specific ('not a png/jpeg', 'too large', ...) rather than a
    generic failure like the render-time sticker fetch."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None, f"couldn't fetch that URL (HTTP {resp.status})"
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type not in CUSTOM_BG_ALLOWED_CONTENT_TYPES:
                return None, f"that URL isn't a png/jpeg (got `{content_type or 'unknown type'}`)"
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > CUSTOM_BG_MAX_BYTES:
                return None, f"that image is over the {CUSTOM_BG_MAX_BYTES // (1024 * 1024)}MB limit"
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > CUSTOM_BG_MAX_BYTES:
                    return None, f"that image is over the {CUSTOM_BG_MAX_BYTES // (1024 * 1024)}MB limit"
            return bytes(data), None
    except Exception as e:
        logger.warning(f"[v0] Couldn't fetch custom background from {url}: {e}")
        return None, "couldn't fetch that URL — make sure it's a direct link to the image file"


async def _get_image_host_channel(bot: commands.Bot) -> discord.TextChannel | None:
    """Resolves the channel /welcome custombg's `image` upload re-posts
    to, so that channel doubles as free image hosting. DB setting (set via
    the owner-only /hostingchannel command) takes priority over the
    IMAGE_HOST_CHANNEL_ID env var, which is just a bootstrap default."""
    channel_id_str = await db.get_global_setting("image_host_channel_id")
    channel_id = int(channel_id_str) if channel_id_str and channel_id_str.isdigit() else bot_config.IMAGE_HOST_CHANNEL_ID
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def _upload_custom_bg(
    bot: commands.Bot, attachment: discord.Attachment, guild: discord.Guild
) -> tuple[int | None, int | None, str | None, str | None]:
    """Validates an uploaded background and re-posts it to the bot's
    image-hosting channel. Returns (channel_id, message_id, cdn_url,
    reason) — cdn_url is just an initial cache; channel_id/message_id are
    what actually get stored, since attachment CDN links are signed and
    expire (~24h) while the message itself (and a freshly re-fetched
    attachment URL from it) does not. On failure, everything but reason is
    None, same contract as _fetch_custom_bg_bytes."""
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type not in CUSTOM_BG_ALLOWED_CONTENT_TYPES:
        return None, None, None, f"that file isn't a png/jpeg (got `{content_type or 'unknown type'}`)"
    if attachment.size > CUSTOM_BG_MAX_BYTES:
        return None, None, None, f"that image is over the {CUSTOM_BG_MAX_BYTES // (1024 * 1024)}MB limit"

    host_channel = await _get_image_host_channel(bot)
    if host_channel is None:
        return None, None, None, (
            "image uploads aren't set up yet — the bot owner needs to run `/hostingchannel` "
            "in a channel first (or you can paste a direct image URL instead)"
        )

    try:
        image_bytes = await attachment.read()
        file = discord.File(io.BytesIO(image_bytes), filename=attachment.filename)
        posted = await host_channel.send(
            content=f"Custom welcome background — guild `{guild.id}` ({guild.name})",
            file=file,
        )
    except discord.HTTPException as e:
        logger.warning(f"[v0] Couldn't upload custom background to hosting channel: {e}")
        return None, None, None, "couldn't upload that image right now — try again in a moment"

    cdn_url = posted.attachments[0].url if posted.attachments else None
    return host_channel.id, posted.id, cdn_url, None


async def _refresh_custom_bg_url(bot: commands.Bot, config_row: dict) -> str | None:
    """Re-fetches the hosting message to get a live (non-expired)
    attachment URL. Returns None if the message/channel is gone."""
    channel_id = config_row.get("custom_bg_channel_id")
    message_id = config_row.get("custom_bg_message_id")
    if not channel_id or not message_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.HTTPException, discord.NotFound):
        return None
    return message.attachments[0].url if message.attachments else None


async def _custom_bg_bytes_for_render(
    session: aiohttp.ClientSession, config_row: dict, bot: commands.Bot | None = None
) -> bytes | None:
    """Render-time counterpart to the strict fetch above: best-effort,
    silent-fallback (same shape as _fetch_sticker_bytes) so a since-removed
    or now-unreachable custom background never fails a join event — it
    just renders with the stock theme background instead for that join.
    Prefers re-fetching a live URL from the hosting message (uploaded
    backgrounds) since the cached custom_background_url for those can be a
    since-expired signed CDN link; falls back to the stored URL either way
    (covers pasted-URL backgrounds, and uploaded ones if the hosting
    message/channel has since been deleted but the last-known link still
    happens to work)."""
    if not config_row.get("ultra_pack_unlocked"):
        return None
    url = None
    if bot is not None:
        url = await _refresh_custom_bg_url(bot, config_row)
    if not url:
        url = (config_row.get("custom_background_url") or "").strip()
    if not url:
        return None
    data, _reason = await _fetch_custom_bg_bytes(session, url)
    return data


def _suggested_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Best-effort guess for where to post welcome cards, so the nudge DM
    can propose a concrete channel instead of asking the owner to pick one
    blind. Prefers the guild's configured system channel (where Discord's
    own join messages go), then falls back to a name match, then the
    first channel the bot can actually post in."""
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for hint in DEFAULT_CHANNEL_NAME_HINTS:
        for channel in guild.text_channels:
            if hint in channel.name.lower() and channel.permissions_for(guild.me).send_messages:
                return channel
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
    return None


class StickerAnnounceView(discord.ui.View):
    """Sent once to the owner of a guild that ALREADY had welcome cards
    enabled, letting them know cards can now include an animated sticker
    (which is already live for them, since card_style defaults to 'gif'
    and sticker_url now defaults to a real working GIF). Same
    fixed-custom_id-on-buttons pattern as WelcomeNudgeView, for the same
    reason: needs to survive a bot restart between send and tap."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Looks good", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"sticker_announce_ack:{guild_id}",
        ))
        self.add_item(discord.ui.Button(
            label="Turn sticker off", style=discord.ButtonStyle.secondary, emoji="🚫",
            custom_id=f"sticker_announce_disable:{guild_id}",
        ))


class TemplateAnnounceView(discord.ui.View):
    """Sent once to the owner of a guild whose welcome card is still the
    plain flat-color card (either because they customized it before the
    designed template existed, or because they've customized colors/shape
    since) — invites them to try the new welcome_bg_wolf.png template card.
    Purely opt-in: tapping "No thanks" leaves their existing card exactly
    as it is, and set_welcome_config never overrides it again once a
    choice is recorded here. Same fixed-custom_id pattern as
    StickerAnnounceView, for the same restart-survival reason."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Try the new look", style=discord.ButtonStyle.success, emoji="✨",
            custom_id=f"template_announce_try:{guild_id}",
        ))
        self.add_item(discord.ui.Button(
            label="Keep my card", style=discord.ButtonStyle.secondary, emoji="🚫",
            custom_id=f"template_announce_decline:{guild_id}",
        ))


class WelcomeNudgeView(discord.ui.View):
    """Sent to the server owner's DMs when a guild has never turned on
    welcome cards. Approve enables it immediately with the suggested
    channel/template; Deny records that so the owner isn't asked again
    every cycle. No callbacks live on the buttons themselves — the
    custom_id encodes guild_id/channel_id so WelcomeCog.on_interaction can
    handle a tap on an old DM even after a bot restart, the same pattern
    ArchiveCog uses for its vote button (decorator-bound callbacks can't
    survive process restarts; bot.add_view() only reattaches views with
    fixed custom_ids, and this view's state is per-guild, not fixed)."""

    def __init__(self, guild_id: int, channel_id: int | None, template: str):
        super().__init__(timeout=None)
        channel_part = channel_id if channel_id else 0
        self.add_item(discord.ui.Button(
            label="Turn on welcome cards", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"welcome_nudge_approve:{guild_id}:{channel_part}",
        ))
        self.add_item(discord.ui.Button(
            label="Not now", style=discord.ButtonStyle.secondary, emoji="✖️",
            custom_id=f"welcome_nudge_deny:{guild_id}",
        ))
        # Kept for reference/display only — approval now reads the live
        # message_template from the DB row (set by _send_nudge, and
        # possibly since changed by an Edit tap) rather than this value.
        self.template = template
        self.add_item(discord.ui.Button(
            label="Edit message", style=discord.ButtonStyle.secondary, emoji="📝",
            custom_id=f"welcome_nudge_edit:{guild_id}",
        ))
        self.add_item(discord.ui.Button(
            label="Ultra Pack", style=discord.ButtonStyle.primary, emoji="✨",
            custom_id=f"welcome_nudge_ultra:{guild_id}",
        ))


class WelcomeNudgeEditModal(discord.ui.Modal, title="Edit welcome message"):
    template = discord.ui.TextInput(
        label="Message ({member} {guild} {count})",
        style=discord.TextStyle.paragraph, max_length=300,
    )

    def __init__(self, guild_id: int, channel_id: int | None, current_template: str):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.template.default = current_template

    async def on_submit(self, interaction: discord.Interaction):
        # Ack first, same reason as the approve button below: the DB
        # write can outrun Discord's 3-second window, which is what was
        # causing "The application didn't respond in time" here too even
        # though the edit was going through.
        await interaction.response.defer()
        clone_id = getattr(interaction.client, "clone_id", None)
        new_template = str(self.template.value).strip()
        # Saved with enabled left as-is (still False until Approve is
        # tapped) so the row exists and Approve later just flips enabled
        # on, picking up this exact edited wording — no re-derivation.
        await db.set_welcome_config(self.guild_id, clone_id=clone_id, message_template=new_template)
        guild = interaction.client.get_guild(self.guild_id)
        preview_text = new_template
        if guild:
            member = guild.get_member(interaction.user.id) or interaction.user
            preview_text = _apply_template(new_template, member) if hasattr(member, "mention") else new_template
        view = WelcomeNudgeView(self.guild_id, self.channel_id, new_template)
        await interaction.edit_original_response(
            content=f"Updated. Here's the message it'll send:\n\n{preview_text}",
            view=view,
        )


class WelcomeCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._nudge_owners.start()
        self._announce_sticker_feature.start()
        self._announce_template_feature.start()

    async def cog_unload(self):
        self._nudge_owners.cancel()
        self._announce_sticker_feature.cancel()
        self._announce_template_feature.cancel()

    # ---- auto-posted setup wizard, fired from bot.py's on_guild_join ----

    async def post_setup_wizard_on_join(self, guild: discord.Guild):
        """Posts the /welcome setup wizard directly in-channel the moment
        the bot joins — no command needed. Unlike the DM-based quickstart
        (quickstart.py), this is visible to the whole server and usable
        by anyone with Manage Server (invoker_id=None on every dynamic
        item in the wizard), not just whoever happens to read their DMs.
        Best-effort: a guild
        with no postable channel, or a closed system channel, is skipped
        silently rather than blocking the join handler."""
        channel = _suggested_channel(guild)
        if not channel:
            return
        try:
            clone_id = getattr(self.bot, "clone_id", None)
            config = await db.get_welcome_config(guild.id, clone_id=clone_id)
            view = build_wizard_view(guild.id, clone_id, None, config)
            message = await channel.send(
                content=(
                    f"👋 Thanks for adding me to **{guild.name}**! Let's start with this first — "
                    f"set up your welcome cards below (anyone with **Manage Server** can use this):"
                ),
                view=view,
            )
            await db.set_welcome_wizard_pointer(guild.id, message.channel.id, message.id, None, clone_id=clone_id)
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.info(f"[v0] Auto-posted setup wizard skipped for guild {guild.id}: {e}")
        except Exception as e:
            logger.error(f"[v0] Auto-posted setup wizard failed for guild {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("welcome_nudge_approve:"):
            _, guild_id_s, channel_id_s = custom_id.split(":")
            guild_id, channel_id = int(guild_id_s), int(channel_id_s)
            if not channel_id:
                await interaction.response.send_message(
                    "I couldn't find a channel I can post in for that server anymore — "
                    "run `/welcome enable` there directly once you've picked one.", ephemeral=True,
                )
                return
            # Ack immediately — the three DB round-trips below can add up
            # to more than Discord's 3-second interaction window,
            # especially on a cold DB connection, which is what was
            # causing "The application didn't respond in time" even
            # though the change was going through. Deferring first buys
            # up to 15 minutes to finish the writes and edit afterward.
            await interaction.response.defer()
            clone_id = getattr(self.bot, "clone_id", None)
            # Read the row instead of recomputing a fresh suggestion — an
            # Edit tap may have already changed message_template, and
            # Approve must not silently discard that.
            config = await db.get_welcome_config(guild_id, clone_id=clone_id)
            await db.set_welcome_config(
                guild_id, clone_id=clone_id, enabled=True, channel_id=channel_id,
                message_template=config.get("message_template"),
            )
            await db.set_welcome_nudge_status(guild_id, "approved", clone_id=clone_id)
            await interaction.edit_original_response(
                content="✅ Welcome cards are on. Fine-tune the message or colors anytime with "
                        "`/welcome message` or `/welcome colors`.",
                view=None, attachments=[],
            )
        elif custom_id.startswith("welcome_nudge_edit:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            config = await db.get_welcome_config(guild_id, clone_id=clone_id)
            channel_id = config.get("channel_id")
            await interaction.response.send_modal(
                WelcomeNudgeEditModal(guild_id, channel_id, config.get("message_template") or "")
            )
        elif custom_id.startswith("welcome_nudge_deny:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            # Deferred first for the same reason as approve/edit above —
            # any one of these DB calls can outrun Discord's 3-second
            # window and surface as "didn't respond in time" even when
            # the write itself succeeds.
            await interaction.response.defer()
            await db.set_welcome_nudge_status(guild_id, "denied", clone_id=clone_id)
            await interaction.edit_original_response(
                content="Got it — won't ask again. Turn it on anytime with `/welcome setup`.",
                view=None, attachments=[],
            )
        elif custom_id.startswith("welcome_nudge_ultra:"):
            guild_id = int(custom_id.split(":", 1)[1])
            await interaction.response.send_message(
                f"✨ **Ultra Pack — ${bot_config.ULTRA_PACK_FEE_USD:g} one-time, unlocks for the whole server**\n\n"
                f"Instead of picking from the preset card themes, upload your own PNG or JPEG (a "
                f"banner, logo, or photo) and every new member's welcome card gets rendered on top of "
                f"it — same name/avatar/member-count layout, your background.\n\n"
                f"Run `/welcome buyultra` in **{interaction.client.get_guild(guild_id).name if interaction.client.get_guild(guild_id) else 'the server'}** "
                f"to purchase, then `/welcome custombg` to upload your image once it's unlocked.",
                ephemeral=True,
            )
        elif custom_id.startswith("sticker_announce_ack:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            await interaction.response.defer()
            await db.set_sticker_announce_status(guild_id, "acknowledged", clone_id=clone_id)
            await interaction.edit_original_response(
                content="✅ Nice — no action needed, it's already live. Change it anytime with "
                        "`/welcome sticker` or `/welcome style`.",
                view=None, attachments=[],
            )
        elif custom_id.startswith("sticker_announce_disable:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            await interaction.response.defer()
            await db.set_welcome_config(guild_id, clone_id=clone_id, card_style="static", sticker_url="")
            await db.set_sticker_announce_status(guild_id, "disabled", clone_id=clone_id)
            await interaction.edit_original_response(
                content="🚫 Turned off — your welcome card is back to the plain version. "
                        "Re-enable anytime with `/welcome sticker <url>`.",
                view=None, attachments=[],
            )
        elif custom_id.startswith("template_announce_try:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            await interaction.response.defer()
            await db.set_welcome_config(guild_id, clone_id=clone_id, use_template=True)
            await db.set_template_announce_status(guild_id, "tried", clone_id=clone_id)
            await interaction.edit_original_response(
                content="✨ Switched on — new members will see the new card starting now. "
                        "Change it back anytime with `/welcome colors`.",
                view=None, attachments=[],
            )
        elif custom_id.startswith("template_announce_decline:"):
            guild_id = int(custom_id.split(":", 1)[1])
            clone_id = getattr(self.bot, "clone_id", None)
            await interaction.response.defer()
            await db.set_template_announce_status(guild_id, "declined", clone_id=clone_id)
            await interaction.edit_original_response(
                content="Got it — your card stays exactly as it is.",
                view=None, attachments=[],
            )

    @tasks.loop(hours=24)
    async def _nudge_owners(self):
        """Once a day, DMs the owner of any guild that has never turned
        welcome cards on — skipping guilds that already said no. The DM
        includes a rendered preview built from that server's own name/
        description/icon, plus Approve/Deny buttons, so turning it on
        takes one tap instead of a wizard."""
        clone_id = getattr(self.bot, "clone_id", None)
        for guild in list(self.bot.guilds):
            try:
                config = await db.get_welcome_config(guild.id, clone_id=clone_id)
                if config.get("enabled"):
                    continue
                if config.get("nudge_status") == "denied":
                    continue
                if config.get("nudge_sent_at"):
                    continue  # already nudged once and not denied — don't spam every cycle
                # Fire-and-forget: _send_nudge holds a 2-minute sleep
                # between its two DMs, and awaiting that inline here would
                # stall every other guild in this pass behind it.
                self.bot.loop.create_task(self._send_nudge(guild, clone_id))
            except Exception as e:
                logger.error(f"[v0] welcome nudge failed for guild {guild.id}: {e}")

    @_nudge_owners.before_loop
    async def _before_nudge_owners(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def _announce_sticker_feature(self):
        """Once, for every guild that ALREADY had welcome cards enabled
        before the sticker feature existed AND is still on the flat card
        (use_template False — the wolf template has no sticker slot at
        all, see render_welcome_card's docstring, so announcing the
        sticker to a template-card guild would show an image that can't
        possibly have one), DMs the owner a preview showing the sticker
        is now live on their card (card_style/sticker_url both default to
        the animated version, so there's nothing to opt into — this is
        purely a heads-up + an easy opt-out). Guilds that turn welcome
        cards on for the FIRST time after this feature shipped don't need
        this — they see the sticker immediately in their normal /welcome
        setup preview, so this loop only targets the backlog of
        already-enabled, still-flat-card guilds."""
        clone_id = getattr(self.bot, "clone_id", None)
        for guild in list(self.bot.guilds):
            try:
                config = await db.get_welcome_config(guild.id, clone_id=clone_id)
                if not config.get("enabled"):
                    continue  # covered by _nudge_owners instead
                if config.get("use_template", True):
                    continue  # wolf card has no sticker slot — nothing to announce
                if config.get("sticker_announce_status"):
                    continue  # owner already acked or disabled it
                if config.get("sticker_announced_at"):
                    continue  # already sent once — don't repeat every cycle
                self.bot.loop.create_task(self._send_sticker_announcement(guild, config, clone_id))
            except Exception as e:
                logger.error(f"[v0] sticker announcement failed for guild {guild.id}: {e}")

    @_announce_sticker_feature.before_loop
    async def _before_announce_sticker_feature(self):
        await self.bot.wait_until_ready()

    async def _send_sticker_announcement(self, guild: discord.Guild, config: dict, clone_id: int | None):
        """Renders the guild's actual current welcome card (with the
        sticker) and DMs it to the owner, once. Marked sent regardless of
        DM success/failure so a closed-DMs owner doesn't get retried
        every cycle forever."""
        try:
            owner = guild.owner or await guild.fetch_member(guild.owner_id)
            if owner is None:
                return
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(str(owner.display_avatar.replace(size=256).url),
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        avatar_bytes = await resp.read()
                    sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
                    custom_bg_bytes = await _custom_bg_bytes_for_render(session, config, self.bot)
                card_bytes, image_format = await asyncio.to_thread(
                    render_welcome_card,
                    avatar_bytes, owner.display_name, f"Member #{guild.member_count}",
                    background_color=config["background_color"], accent_color=config["accent_color"],
                    sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                    guild_name=guild.name, use_template=config.get("use_template", True),
                    theme=config.get("card_theme", "wolf"), custom_background_bytes=custom_bg_bytes,
                )
                ext = "gif" if image_format == "GIF" else "png"
                file = discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}")
            except Exception as e:
                logger.warning(f"[v0] couldn't render sticker-announcement preview for guild {guild.id}: {e}")
                file = None

            dm = await owner.create_dm()
            intro = (
                f"🎉 Heads up — **{guild.name}**'s welcome cards can now include an animated "
                f"sticker, and it's already live (no setup needed). Here's what it looks like now:"
            )
            view = StickerAnnounceView(guild.id)
            if file:
                await dm.send(content=intro, file=file, view=view)
            else:
                await dm.send(content=intro, view=view)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] sticker announcement DM failed for guild {guild.id}: {e}")
        finally:
            await db.mark_sticker_announcement_sent(guild.id, clone_id=clone_id)

    @tasks.loop(hours=24)
    async def _announce_template_feature(self):
        """Once, for every guild whose welcome cards are enabled but still
        on the flat card (use_template False — either backfilled because
        they'd customized it before the template existed, or because
        they've customized colors/shape since), DMs the owner a preview of
        the new template card with a one-tap opt-in. Guilds already on
        use_template True need nothing — they're already seeing it."""
        clone_id = getattr(self.bot, "clone_id", None)
        for guild in list(self.bot.guilds):
            try:
                config = await db.get_welcome_config(guild.id, clone_id=clone_id)
                if not config.get("enabled"):
                    continue  # covered by _nudge_owners instead
                if config.get("use_template"):
                    continue  # already on the new card, nothing to announce
                if config.get("template_announce_status"):
                    continue  # owner already tried it or declined
                if config.get("template_announced_at"):
                    continue  # already sent once — don't repeat every cycle
                self.bot.loop.create_task(self._send_template_announcement(guild, config, clone_id))
            except Exception as e:
                logger.error(f"[v0] template announcement failed for guild {guild.id}: {e}")

    @_announce_template_feature.before_loop
    async def _before_announce_template_feature(self):
        await self.bot.wait_until_ready()

    async def _send_template_announcement(self, guild: discord.Guild, config: dict, clone_id: int | None):
        """Renders a PREVIEW of the new template card (not the guild's
        actual current card, since by definition this guild is still on
        the flat one) and DMs it to the owner, once. Marked sent regardless
        of DM success/failure so a closed-DMs owner doesn't get retried
        every cycle forever."""
        try:
            owner = guild.owner or await guild.fetch_member(guild.owner_id)
            if owner is None:
                return
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(str(owner.display_avatar.replace(size=256).url),
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        avatar_bytes = await resp.read()
                card_bytes, image_format = await asyncio.to_thread(
                    render_welcome_card,
                    avatar_bytes, owner.display_name, f"Member #{guild.member_count}",
                    guild_name=guild.name, use_template=True,
                    theme=config.get("card_theme", "wolf"),
                )
                ext = "gif" if image_format == "GIF" else "png"
                file = discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}")
            except Exception as e:
                logger.warning(f"[v0] couldn't render template-announcement preview for guild {guild.id}: {e}")
                file = None

            dm = await owner.create_dm()
            intro = (
                f"✨ Heads up — there's a new designed welcome card available for **{guild.name}**. "
                f"Your current colors/style are untouched either way — here's a preview of the new look:"
            )
            view = TemplateAnnounceView(guild.id)
            if file:
                await dm.send(content=intro, file=file, view=view)
            else:
                await dm.send(content=intro, view=view)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] template announcement DM failed for guild {guild.id}: {e}")
        finally:
            await db.mark_template_announcement_sent(guild.id, clone_id=clone_id)

    async def _send_nudge(self, guild: discord.Guild, clone_id: int | None):
        """Sends the reminder DM twice, 2 minutes apart, then stops for
        good — whether or not the owner ever responds. A response at any
        later time (Approve/Deny/Edit) is still handled normally by
        on_interaction; this method just controls how many times the DM
        itself goes out. Runs as a fire-and-forget background task (see
        _nudge_owners), so it catches its own errors instead of relying
        on a caller's try/except."""
        try:
            owner = guild.owner or await guild.fetch_member(guild.owner_id)
            if owner is None:
                return
            template = _suggested_template(guild)
            channel = _suggested_channel(guild)

            # Persist the suggested template/channel now (still
            # enabled=False) so Edit and Approve both read/write the same
            # row instead of Approve silently re-deriving a fresh
            # suggestion that would discard whatever the owner just edited.
            await db.set_welcome_config(
                guild.id, clone_id=clone_id, enabled=False,
                channel_id=channel.id if channel else None, message_template=template,
            )

            sent_once = await self._send_nudge_dm(guild, owner, channel, template, clone_id)
            # Mark it sent right after the first attempt (not after the
            # second) so a crash/restart between the two sends can't cause
            # the daily loop to treat this guild as never-nudged and
            # restart the whole two-message sequence from scratch.
            await db.mark_welcome_nudge_sent(guild.id, clone_id=clone_id)
            if not sent_once:
                return  # DMs closed — no point trying a second time

            await asyncio.sleep(120)

            current = await db.get_welcome_config(guild.id, clone_id=clone_id)
            if current.get("enabled") or current.get("nudge_status"):
                return  # owner already acted on the first DM — don't send a second

            await self._send_nudge_dm(guild, owner, channel, template, clone_id)
        except Exception as e:
            logger.error(f"[v0] welcome nudge failed for guild {guild.id}: {e}")

    async def _send_nudge_dm(self, guild: discord.Guild, owner: discord.abc.User,
                              channel: discord.TextChannel | None, template: str,
                              clone_id: int | None) -> bool:
        """Renders and sends a single reminder DM. Returns False if the
        owner's DMs are closed, True otherwise."""
        avatar_bytes = None
        sticker_bytes = None
        custom_bg_bytes = None
        config = await db.get_welcome_config(guild.id, clone_id=clone_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(str(owner.display_avatar.replace(size=256).url),
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
                custom_bg_bytes = await _custom_bg_bytes_for_render(session, config, self.bot)
        except Exception:
            pass

        try:
            dm = await owner.create_dm()
            intro = (
                f"👋 **{guild.name}** doesn't have welcome cards set up yet — new members just "
                f"join quietly with no greeting.\n\nHere's what one would look like for the next "
                f"person who joins:"
            )
            ultra_blurb = (
                f"\n\n✨ **Ultra Pack (${bot_config.ULTRA_PACK_FEE_USD:g} one-time, whole server):** "
                f"use your own PNG/JPEG as the welcome card background instead of a preset theme — "
                f"upload a photo, banner, or logo and every new member's card is rendered on it. "
                f"Tap **Ultra Pack** below to learn more, or run `/welcome buyultra` anytime."
            )
            view = WelcomeNudgeView(guild.id, channel.id if channel else None, template)
            if avatar_bytes:
                # Preview with the sticker/style the guild would actually
                # get if approved — get_welcome_config returns sane
                # defaults (sticker_url + card_style='gif') even before
                # any row exists, so a never-configured guild still sees
                # a representative preview, not a bare card.
                card_bytes, image_format = await asyncio.to_thread(
                    render_welcome_card,
                    avatar_bytes, owner.display_name, "Member #1",
                    background_color=config["background_color"], accent_color=config["accent_color"],
                    sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                    guild_name=guild.name, use_template=config.get("use_template", True),
                    theme=config.get("card_theme", "wolf"), custom_background_bytes=custom_bg_bytes,
                )
                ext = "gif" if image_format == "GIF" else "png"
                file = discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}")
                preview_text = _apply_template(template, owner)
                channel_note = f"\nWould post in {channel.mention}." if channel else "\nI'd need you to pick a channel — no postable channel found."
                await dm.send(content=f"{intro}\n\n{preview_text}{channel_note}{ultra_blurb}", file=file, view=view)
            else:
                await dm.send(content=f"{intro}{ultra_blurb}", view=view)
            return True
        except discord.Forbidden:
            return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_welcome_config(member.guild.id, clone_id=clone_id)
        if not config.get("enabled"):
            return

        dm_mode = config.get("delivery_mode") == "dm"
        channel = None
        if not dm_mode:
            if not config.get("channel_id"):
                return
            channel = member.guild.get_channel(int(config["channel_id"]))
            if channel is None:
                return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(str(member.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
                custom_bg_bytes = await _custom_bg_bytes_for_render(session, config, self.bot)
            card_bytes, image_format = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, member.display_name, f"Member #{member.guild.member_count}",
                background_color=config["background_color"], accent_color=config["accent_color"],
                sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                guild_name=member.guild.name, use_template=config.get("use_template", True),
                theme=config.get("card_theme", "wolf"), custom_background_bytes=custom_bg_bytes,
            )
            ext = "gif" if image_format == "GIF" else "png"
            file = discord.File(fp=__import__("io").BytesIO(card_bytes), filename=f"welcome.{ext}")
            content = _apply_template(config["message_template"], member)

            if dm_mode:
                try:
                    dm = await member.create_dm()
                    await dm.send(content=content, file=file)
                except discord.Forbidden:
                    # Member has DMs closed / blocks the bot — nothing we
                    # can do without a channel fallback the admin didn't
                    # ask for, so just log and move on.
                    logger.info(f"[v0] Couldn't DM welcome card to {member.id} in guild {member.guild.id} (DMs closed).")
            else:
                await channel.send(content=content, file=file)
        except Exception as e:
            logger.error(f"[v0] Failed to render/send welcome card for guild {member.guild.id}: {e}")
            try:
                fallback_text = _apply_template(config["message_template"], member)
                if dm_mode:
                    dm = await member.create_dm()
                    await dm.send(fallback_text)
                else:
                    await channel.send(fallback_text)
            except discord.Forbidden:
                pass

    group = app_commands.guild_only()(app_commands.Group(name="welcome", description="Configure welcome cards for new members"))

    @group.command(name="enable", description="Turn welcome cards on for a channel")
    async def enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), enabled=True, channel_id=channel.id)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Welcome cards enabled in {channel.mention}.", ephemeral=True)

    @group.command(name="disable", description="Turn welcome cards off")
    async def disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), enabled=False)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send("✅ Welcome cards disabled.", ephemeral=True)

    @group.command(name="message", description="Set the welcome text. Placeholders: {member} {guild} {count}")
    async def message(self, interaction: discord.Interaction, template: str):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), message_template=template)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send("✅ Welcome message updated.", ephemeral=True)

    NAMED_COLORS = {
        "dark navy": "#1a1a2e", "blurple": "#5865f2", "black": "#000000",
        "white": "#ffffff", "red": "#e94560", "pink": "#ff6b9d",
        "purple": "#9b59b6", "blue": "#3498db", "teal": "#1abc9c",
        "green": "#2ecc71", "orange": "#e67e22", "yellow": "#f1c40f",
        "gray": "#2b2d31", "discord dark": "#2b2d31",
    }

    async def _color_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=f"{name} ({hex_})", value=hex_)
            for name, hex_ in self.NAMED_COLORS.items() if current in name
        ][:25]

    def _resolve_color(self, value: str) -> str | None:
        key = value.strip().lower()
        if key in self.NAMED_COLORS:
            return self.NAMED_COLORS[key]
        if value.startswith("#") and len(value) == 7:
            return value
        return None

    @group.command(name="colors", description="Set the welcome card's background and accent colors (hex or name, e.g. #2b2d31 or 'blurple')")
    @app_commands.autocomplete(background=_color_autocomplete, accent=_color_autocomplete)
    async def colors(self, interaction: discord.Interaction, background: str, accent: str):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        resolved_bg = self._resolve_color(background)
        resolved_accent = self._resolve_color(accent)
        if resolved_bg is None or resolved_accent is None:
            names = ", ".join(sorted(self.NAMED_COLORS))
            await interaction.followup.send(
                f"Colors must be a hex code (e.g. `#2b2d31`) or one of: {names}.", ephemeral=True
            )
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), background_color=resolved_bg, accent_color=resolved_accent)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Colors updated (`{resolved_bg}` / `{resolved_accent}`).", ephemeral=True)

    @group.command(name="sticker", description="Set (or remove) the GIF/sticker posted after the welcome card")
    async def sticker(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        url = url.strip()
        if url.lower() in ("none", "off", "disable"):
            await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), sticker_url="")
            await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
            await interaction.followup.send("✅ Welcome sticker disabled.", ephemeral=True)
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.followup.send(
                "That doesn't look like a URL. Paste a GIF/Tenor/image link, or `none` to disable.", ephemeral=True
            )
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), sticker_url=url)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Welcome sticker set: {url}", ephemeral=True)

    @group.command(name="buypack", description="Buy the premium welcome-card pack (extra card looks, one-time, unlocks for the whole server)")
    async def buypack(self, interaction: discord.Interaction):
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from discord_bot.views_card_pack import start_card_pack_payment
        await start_card_pack_payment(interaction)

    @group.command(name="buyultra", description="Buy the ultra pack (use your own png/jpeg as the welcome card background, one-time, whole server)")
    async def buyultra(self, interaction: discord.Interaction):
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from discord_bot.views_card_pack import start_ultra_pack_payment
        await start_ultra_pack_payment(interaction)

    @group.command(name="custombg", description="[Ultra pack] Set the welcome card's background to your own png/jpeg")
    @app_commands.describe(
        url="Direct link to a .png or .jpg image (not a page URL) — leave blank to clear it",
        image=f"Upload a .png or .jpg instead of a URL (max {CUSTOM_BG_MAX_BYTES // (1024 * 1024)}MB)",
    )
    async def custombg(self, interaction: discord.Interaction, url: str = "", image: discord.Attachment = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return

        config = await db.get_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not config.get("ultra_pack_unlocked"):
            await interaction.followup.send(
                "Custom backgrounds are part of the ultra pack — this server hasn't bought it yet. "
                "Run `/welcome buyultra` to unlock it for good.",
                ephemeral=True,
            )
            return

        url = url.strip()
        if image is not None and url:
            await interaction.followup.send("⚠️ Use either `url` or `image`, not both.", ephemeral=True)
            return

        if not url and image is None:
            await db.set_welcome_config(
                interaction.guild_id, clone_id=_clone_id_of(interaction),
                custom_background_url=None, custom_bg_channel_id=None, custom_bg_message_id=None,
            )
            await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
            await interaction.followup.send(
                f"✅ Custom background cleared — back to the **{config.get('card_theme', 'wolf')}** theme.",
                ephemeral=True,
            )
            return

        if image is not None:
            host_channel_id, host_message_id, cdn_url, reason = await _upload_custom_bg(
                self.bot, image, interaction.guild
            )
            if reason:
                await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                return
            # Selecting a custom background implies the template card, same
            # reasoning set_welcome_config already applies for colors/theme.
            await db.set_welcome_config(
                interaction.guild_id, clone_id=_clone_id_of(interaction),
                custom_background_url=cdn_url,
                custom_bg_channel_id=host_channel_id, custom_bg_message_id=host_message_id,
            )
            await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
            await interaction.followup.send("✅ Welcome card background set to your uploaded image.", ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            image_bytes, reason = await _fetch_custom_bg_bytes(session, url)
        if image_bytes is None:
            await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
            return

        await db.set_welcome_config(
            interaction.guild_id, clone_id=_clone_id_of(interaction),
            custom_background_url=url, custom_bg_channel_id=None, custom_bg_message_id=None,
        )
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Welcome card background set to your image: {url}", ephemeral=True)

    @app_commands.command(name="hostingchannel", description="[Bot owner] Use THIS channel to host images uploaded via /welcome custombg")
    async def hostingchannel(self, interaction: discord.Interaction):
        if interaction.user.id not in bot_config.DISCORD_OWNER_BROADCAST_IDS:
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run this in a text channel — that's the one that'll store uploads.", ephemeral=True)
            return
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files and perms.read_message_history):
            await interaction.response.send_message(
                "I need Send Messages, Attach Files, and Read Message History in this channel to use it for hosting.",
                ephemeral=True,
            )
            return
        await db.set_global_setting("image_host_channel_id", str(interaction.channel.id))
        await interaction.response.send_message(
            f"✅ Uploaded custom backgrounds (`/welcome custombg`'s `image` option) will now be stored in "
            f"#{interaction.channel.name}. Keep this channel private and don't delete old messages in it — "
            f"each guild's background lives in one message here.",
            ephemeral=True,
        )

    @group.command(name="theme", description="Pick which welcome-card look this server uses")
    @app_commands.choices(look=[
        app_commands.Choice(name="Wolf (free, default)", value="wolf"),
        app_commands.Choice(name="Metallic Reaper (premium)", value="reaper"),
        app_commands.Choice(name="Shadow Monarch (premium)", value="shadow"),
        app_commands.Choice(name="Emerald Sorcerer (premium)", value="sorcerer"),
    ])
    async def theme(self, interaction: discord.Interaction, look: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return

        from modules.welcome_card import PREMIUM_THEMES
        if look.value in PREMIUM_THEMES:
            config = await db.get_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
            if not config.get("card_pack_unlocked"):
                await interaction.followup.send(
                    f"**{look.name}** is part of the premium card pack — this server hasn't bought it yet. "
                    f"Run `/welcome buypack` to unlock every premium look for good.",
                    ephemeral=True,
                )
                return

        # Selecting any theme (including the free 'wolf') implies the
        # designed-background card, not the flat color card — same reasoning
        # set_welcome_config already applies when colors are touched.
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), card_theme=look.value, use_template=True)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Welcome card look set to **{look.name}**.", ephemeral=True)

    @group.command(name="style", description="Switch the welcome card between an animated sticker or a static image")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Animated — sticker dances in the card", value="gif"),
        app_commands.Choice(name="Static — plain image, no animation", value="static"),
    ])
    async def style(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        await db.set_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction), card_style=mode.value)
        await refresh_posted_wizard(self.bot, interaction.guild_id, _clone_id_of(interaction))
        await interaction.followup.send(f"✅ Welcome card style set to **{mode.name}**.", ephemeral=True)

    @group.command(name="setup", description="Set up welcome cards with an interactive wizard")
    async def setup_wizard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        config = await db.get_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        view = build_wizard_view(interaction.guild_id, _clone_id_of(interaction), interaction.user.id, config)
        # Posted publicly in-channel (not ephemeral) so anyone in the
        # server can see the wizard being configured — only the original
        # invoker can actually use its components, enforced by each
        # dynamic item's _check_access call (invoker_id is baked into
        # every component's custom_id).
        await interaction.followup.send(view=view)
        message = await interaction.original_response()
        await db.set_welcome_wizard_pointer(
            interaction.guild_id, message.channel.id, message.id, interaction.user.id,
            clone_id=_clone_id_of(interaction),
        )

    @group.command(name="preview", description="Preview the welcome card as it would look for you")
    async def preview(self, interaction: discord.Interaction):
        config = await db.get_welcome_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(str(interaction.user.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
                custom_bg_bytes = await _custom_bg_bytes_for_render(session, config, self.bot)
            card_bytes, image_format = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, interaction.user.display_name, f"Member #{interaction.guild.member_count}",
                background_color=config["background_color"], accent_color=config["accent_color"],
                sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                guild_name=interaction.guild.name, use_template=config.get("use_template", True),
                theme=config.get("card_theme", "wolf"), custom_background_bytes=custom_bg_bytes,
            )
            ext = "gif" if image_format == "GIF" else "png"
            file = discord.File(fp=__import__("io").BytesIO(card_bytes), filename=f"preview.{ext}")
            buttons = [refresh_button(self, "preview")]
            lines = [_apply_template(config["message_template"], interaction.user), "-# Preview only — visible to you"]
            text = discord.ui.TextDisplay("\n".join(["### Welcome preview", *lines]))
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(file))
            row = discord.ui.ActionRow()
            for b in buttons:
                row.add_item(b)
            view = discord.ui.LayoutView(timeout=180)
            view.add_item(discord.ui.Container(text, gallery, discord.ui.Separator(), row, accent_colour=discord.Color.blurple()))
            await interaction.followup.send(view=view, file=file, ephemeral=True)
        except Exception as e:
            logger.error(f"[v0] Welcome card preview failed: {e}")
            await interaction.followup.send("Couldn't render a preview — check logs.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
