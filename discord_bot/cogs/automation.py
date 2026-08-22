"""
Automation polish (Phase 4) — the "and even more automated" part of the
expansion spec. Two pieces live here:

  - /autoresponder: simple trigger -> response pairs, checked against every
    non-bot message via case-insensitive substring match. Deliberately not
    regex — this is meant to be configurable by a non-technical server
    admin via slash command, not something that needs escaping/testing.
  - /serversetup: a guided wizard (buttons, not a slash-command essay) that
    walks a new admin through enabling reaction roles / automod / welcome /
    leveling in one flow, rather than them discovering each feature's own
    slash command separately. The spec calls this out as likely the single
    highest-leverage "feels more automated than the competition" feature,
    so it gets first-class treatment here rather than just linking to docs.

Scheduled announcements (the third Phase 4 piece) are configured here via
/announce, but actually SENT by api/cron_discord_announcements.py over
Discord's REST API — see that file's docstring for why a live gateway cog
isn't the sender (this bot's gateway process isn't guaranteed to be the one
running when a scheduled post comes due, especially for clones).

i18n: bot-authored UI strings go through discord_bot.i18n_helpers.tr() (see
economy.py's module docstring for how the translation cache works).
User-authored content — autoresponder trigger/response text, announcement
message bodies — is sent as-is; it's the admin's own freeform text, not
this bot's copy, so translating it isn't this cog's call to make.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.i18n_helpers import get_lang, tr
from discord_bot.cogs._views_shared import NavCardView, refresh_button

logger = logging.getLogger(__name__)


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


async def _deny(interaction: discord.Interaction, perm_name: str, lang: str):
    msg = await tr("You need the **{perm_name}** permission to do that.", lang, perm_name=perm_name)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class ServerSetupView(discord.ui.View):
    """Not persistent (custom_ids aren't fixed/global) — this wizard is only
    ever opened fresh from /serversetup, unlike the premium/reaction-role
    views which must survive a restart because they live on old messages."""

    def __init__(self, clone_id, lang: str):
        super().__init__(timeout=300)
        self.clone_id = clone_id
        self.lang = lang

    @discord.ui.button(label="Enable Auto-Moderation", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def enable_automod(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await db.set_automod_config(interaction.guild_id, clone_id=self.clone_id, word_filter_enabled=True,
                                     anti_invite_enabled=True, spam_enabled=True)
        msg = await tr(
            "🛡️ Auto-mod enabled (word filter, invite filter, spam filter). Fine-tune with `/automod`.",
            self.lang
        )
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Enable Welcome Messages", style=discord.ButtonStyle.primary, emoji="👋")
    async def enable_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await db.set_welcome_config(interaction.guild_id, clone_id=self.clone_id, enabled=True,
                                     channel_id=interaction.channel_id)
        msg = await tr(
            "👋 Welcome messages enabled in {channel}. Customize with `/setwelcome`.",
            self.lang, channel=interaction.channel.mention
        )
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Enable Leveling", style=discord.ButtonStyle.primary, emoji="📈")
    async def enable_leveling(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Leveling has no on/off flag of its own — it's active as soon as
        # the LevelingCog's on_message listener is loaded, which it always
        # is (see bot.py's setup_hook). This button exists so the wizard
        # covers the full feature set in one place; it just confirms and
        # points at /rank and /levelrole rather than flipping a switch.
        msg = await tr(
            "📈 Leveling is already active — members earn XP as they chat. "
            "Set up role rewards with `/levelrole add`.", self.lang
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Enable Economy", style=discord.ButtonStyle.primary, emoji="💰")
    async def enable_economy(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = await tr(
            "💰 The economy game is already active — try `/daily`, `/work`, `/shop list`. "
            "Configure currency name/bonuses with `/ecoconfig`.", self.lang
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        msg = await tr("✅ Setup complete!", self.lang)
        await interaction.response.edit_message(content=msg, view=self)
        self.stop()


class AutomationCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        responders = await db.get_autoresponders(message.guild.id, clone_id=clone_id)
        if not responders:
            return
        content_lower = message.content.lower()
        for r in responders:
            if r["trigger"] in content_lower:
                try:
                    await message.channel.send(r["response"])
                except discord.Forbidden:
                    pass
                break  # only fire the first match per message, not every trigger it contains

    @app_commands.command(name="serversetup", description="Guided setup wizard for this bot's features")
    @app_commands.guild_only()
    async def serversetup(self, interaction: discord.Interaction):
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        msg = await tr(
            "**Welcome to setup!** Tap each feature you want to turn on — you can always "
            "reconfigure later with its own slash command.", lang
        )
        await interaction.response.send_message(
            msg, view=ServerSetupView(_clone_id_of(interaction), lang), ephemeral=True
        )

    autoresponder_group = app_commands.guild_only()(app_commands.Group(name="autoresponder", description="Manage auto-responses"))

    @autoresponder_group.command(name="add", description="Add an auto-response trigger")
    async def ar_add(self, interaction: discord.Interaction, trigger: str, response: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ar_id = await db.add_autoresponder(interaction.guild_id, trigger, response, interaction.user.id,
                                            clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Added auto-responder #{id} for `{trigger}`.", lang, id=ar_id, trigger=trigger)
        await interaction.followup.send(msg, ephemeral=True)

    @autoresponder_group.command(name="remove", description="Remove an auto-response")
    async def ar_remove(self, interaction: discord.Interaction, autoresponder_id: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ok = await db.remove_autoresponder(interaction.guild_id, autoresponder_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Removed.", lang) if ok else await tr("No such auto-responder.", lang)
        await interaction.followup.send(msg, ephemeral=True)

    @autoresponder_group.command(name="list", description="List auto-responses")
    async def ar_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        rows = await db.get_autoresponders(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not rows:
            msg = await tr("No auto-responders configured.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = [f"**#{r['id']} — {r['trigger']}**\n{r['response'][:80]}" for r in rows]
        buttons = [refresh_button(self, "ar_list")]
        card = NavCardView("Auto-responders", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="announce", description="Schedule an announcement in a channel")
    @app_commands.guild_only()
    @app_commands.describe(
        channel="Channel to post in", message="What to post",
        in_minutes="Send this many minutes from now",
        repeat_every_minutes="Optional: repeat every N minutes after that"
    )
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str,
                        in_minutes: app_commands.Range[int, 0, None] = 0,
                        repeat_every_minutes: app_commands.Range[int, 5, None] = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        next_run_at = datetime.now(timezone.utc) + timedelta(minutes=in_minutes)
        ann_id = await db.add_scheduled_announcement(
            interaction.guild_id, channel.id, message, next_run_at, interaction.user.id,
            interval_minutes=repeat_every_minutes, clone_id=_clone_id_of(interaction)
        )
        now_word = await tr("now", lang)
        in_minutes_word = await tr("in {minutes}m", lang, minutes=in_minutes) if in_minutes else now_word
        repeat_note = await tr(", repeating every {minutes}m", lang, minutes=repeat_every_minutes) \
            if repeat_every_minutes else ""
        msg = await tr(
            "✅ Scheduled announcement #{id} in {channel} ({when}{repeat_note}). "
            "Delivery runs on the external cron — see api/cron_discord_announcements.py.", lang,
            id=ann_id, channel=channel.mention, when=in_minutes_word, repeat_note=repeat_note
        )
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="announcements", description="List this server's scheduled announcements")
    @app_commands.guild_only()
    async def announcements(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        rows = await db.get_scheduled_announcements(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not rows:
            msg = await tr("No scheduled announcements.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = []
        for r in rows:
            when = f"{r['next_run_at']:%Y-%m-%d %H:%M UTC}"
            if r["interval_minutes"]:
                when += f" (every {r['interval_minutes']}m)"
            lines.append(f"**#{r['id']} — <#{r['channel_id']}>**\n{when}")
        buttons = [refresh_button(self, "announcements")]
        card = NavCardView("Scheduled announcements", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="cancelannouncement", description="Cancel a scheduled announcement")
    @app_commands.guild_only()
    async def cancel_announcement(self, interaction: discord.Interaction, announcement_id: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ok = await db.remove_scheduled_announcement(interaction.guild_id, announcement_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Cancelled.", lang) if ok else await tr("No such announcement.", lang)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutomationCog(bot))
