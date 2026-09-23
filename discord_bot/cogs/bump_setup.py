# path: discord_bot/cogs/bump_setup.py

"""
/bumpsetup — configure this server's bump-network settings entirely
through buttons/selects, no typed arguments.

`/bumpsetup` was already referenced from help.py, analytics.py,
setup_channels.py and the "Enable Bump Network" toggle's own
confirmation message, but the command itself was never implemented —
this fills that gap, following the same invoker-gated WizardView
pattern already used by /setupverification (verification.py) and the
invites setup flow (_views_invites.py): one ephemeral message, a few
selects/buttons that mutate in-memory wizard state, and a Finish
button that writes it all to the DB in one call.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from database import db

logger = logging.getLogger(__name__)

# intensity_level isn't matched against anything yet (bump_find_targets
# doesn't filter on it) — it's a plain 1-5 scale for how often/energetic
# this server wants incoming bump posts to be, stored for future use by
# whatever schedules bump_enqueue's drip_seconds per guild.
INTENSITY_LABELS = {
    1: "Low — occasional bumps only",
    2: "Light",
    3: "Normal (default)",
    4: "Frequent",
    5: "High — as many as the network sends",
}

# 'any' (the DB default) matches every other guild's language regardless
# of what they've set, per bump_find_targets. Kept short and specific to
# what a bump audience would plausibly filter on.
LANGUAGE_OPTIONS = [
    ("any", "Any language (default)"),
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
    ("ar", "Arabic"),
]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _create_bump_channel(interaction: discord.Interaction, guild: discord.Guild = None, user=None) -> discord.TextChannel:
    """Creates a bot-posting-only #bump channel (in the "Server Setup"
    category if the guild already has one). Raises discord.Forbidden /
    discord.HTTPException on failure — callers show their own message.

    `guild`/`user` are optional overrides for callers that run from a DM
    (the join-DM "Partnership" button), where interaction.guild is None."""
    guild = guild or interaction.guild
    user = user or interaction.user
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(send_messages=False),
        guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True, manage_messages=True),
    }
    category = discord.utils.get(guild.categories, name="📋 Server Setup")
    channel = await guild.create_text_channel(
        "bump",
        category=category,
        overwrites=overwrites,
        reason=f"Auto-created by /bumpsetup for {user}",
    )
    try:
        await channel.send(
            "📣 Bump reminders and your server's listing will appear here. "
            "Run `/bump now` to send your server out to the network."
        )
    except discord.HTTPException:
        pass
    return channel


async def ensure_bot_can_post(channel: discord.TextChannel) -> bool:
    """Makes sure THIS bot can view/post embeds in `channel`. With a shared
    bump network several bots (main + clones) deliver into the same #bump,
    and a channel another bot created is bot-only for that bot — so grant
    this bot access if it's missing. Returns False if it still can't post."""
    me = channel.guild.me

    def _ok():
        p = channel.permissions_for(me)
        return p.view_channel and p.send_messages and p.embed_links

    if _ok():
        return True
    try:
        await channel.set_permissions(
            me, send_messages=True, embed_links=True, view_channel=True,
            reason="Bump network: let this bot post in the shared #bump",
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    return _ok()


class BumpChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="1. Choose the bump channel",
            channel_types=[discord.ChannelType.text],
            default_values=[discord.Object(id=wizard.channel_id)] if wizard.channel_id else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.channel_id = self.values[0].id
        self.default_values = self.values
        await self.wizard.refresh(interaction)


class BumpLanguageSelect(discord.ui.Select):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="2. Choose a language filter",
            options=[
                discord.SelectOption(label=label, value=code, default=(code == wizard.language))
                for code, label in LANGUAGE_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.language = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == self.wizard.language)
        await self.wizard.refresh(interaction)


class BumpIntensitySelect(discord.ui.Select):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="3. Choose how often you want bumps",
            options=[
                discord.SelectOption(label=label, value=str(level), default=(level == wizard.intensity_level))
                for level, label in INTENSITY_LABELS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.intensity_level = int(self.values[0])
        for opt in self.options:
            opt.default = (int(opt.value) == self.wizard.intensity_level)
        await self.wizard.refresh(interaction)


class BumpNsfwToggleButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            label=self._label(wizard.nsfw_opt_in),
            style=self._style(wizard.nsfw_opt_in),
            row=3,
        )

    @staticmethod
    def _label(on: bool) -> str:
        return "NSFW bumps: ON — tap to turn off" if on else "NSFW bumps: OFF — tap to turn on"

    @staticmethod
    def _style(on: bool) -> discord.ButtonStyle:
        return discord.ButtonStyle.danger if on else discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction):
        self.wizard.nsfw_opt_in = not self.wizard.nsfw_opt_in
        self.label = self._label(self.wizard.nsfw_opt_in)
        self.style = self._style(self.wizard.nsfw_opt_in)
        await self.wizard.refresh(interaction)


class BumpCreateChannelButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(label="➕ Create #bump for me", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission to create one — grant it, or pick an existing channel above.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            channel = await _create_bump_channel(interaction)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning("[bumpsetup] couldn't create #bump in guild %s: %s", interaction.guild_id, e)
            await interaction.followup.send(
                "Couldn't create the channel — pick an existing one above instead.", ephemeral=True,
            )
            return
        self.wizard.channel_id = channel.id
        self.wizard.created_channel_id = channel.id
        for item in self.wizard.children:
            if isinstance(item, BumpChannelSelect):
                item.default_values = [discord.Object(id=channel.id, type=discord.abc.GuildChannel)]
        await interaction.edit_original_response(embed=self.wizard.build_embed(), view=self.wizard)


class BumpFinishButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(label="✅ Save", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if not wizard.channel_id:
            # Nothing picked — create #bump automatically instead of blocking.
            if not interaction.guild.me.guild_permissions.manage_channels:
                await interaction.response.send_message(
                    "Pick a bump channel first — I don't have **Manage Channels**, so I can't create one for you.",
                    ephemeral=True,
                )
                return
            try:
                channel = await _create_bump_channel(interaction)
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("[bumpsetup] auto-create #bump failed in guild %s: %s", interaction.guild_id, e)
                await interaction.response.send_message(
                    "Couldn't create a #bump channel — pick an existing one above.", ephemeral=True,
                )
                return
            wizard.channel_id = channel.id
            wizard.created_channel_id = channel.id
        chosen = interaction.guild.get_channel(wizard.channel_id)
        if isinstance(chosen, discord.TextChannel):
            await ensure_bot_can_post(chosen)  # best-effort; shared #bump may belong to another bot
        await db.bump_set_guild_config(
            guild_id=interaction.guild_id,
            clone_id=_clone_id_of(interaction),
            configured_by=interaction.user.id,
            bump_channel_id=wizard.channel_id,
            language=wizard.language,
            nsfw_opt_in=wizard.nsfw_opt_in,
            intensity_level=wizard.intensity_level,
            receives_bumps=True,
        )
        if wizard.created_channel_id and wizard.created_channel_id == wizard.channel_id:
            try:
                from database import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE bump_guild_config SET channel_auto_created = TRUE "
                        "WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)",
                        interaction.guild_id, _clone_id_of(interaction),
                    )
            except Exception:
                logger.exception("[bumpsetup] couldn't flag channel_auto_created for guild %s", interaction.guild_id)
        # /bump now needs a server listing to exist. Create the suggested
        # one (description/tags/perks pulled from the guild) if there isn't
        # one yet — never overwrites an existing listing.
        footer = "Saved ✅"
        try:
            clone_id = _clone_id_of(interaction)
            existing = await db.bump_get_listing(interaction.guild_id, clone_id, "server")
            if not existing:
                from discord_bot.cogs.bump import _suggest_description_and_tags, _suggest_perks
                desc, tags = _suggest_description_and_tags(interaction.guild)
                perks = _suggest_perks(interaction.guild)
                invite_url = await interaction.client._best_effort_invite(interaction.guild)
                await db.bump_upsert_listing(
                    guild_id=interaction.guild_id,
                    clone_id=clone_id,
                    created_by=interaction.user.id,
                    listing_type="server",
                    name=interaction.guild.name,
                    description=desc,
                    invite_url=invite_url,
                    tags=tags,
                    perks=perks,
                )
                footer = "Saved ✅ — listing created. Change it anytime with /bump edit"
        except Exception:
            logger.exception("[bumpsetup] auto-listing failed for guild %s", interaction.guild_id)
            footer = "Saved ✅ — couldn't auto-create your listing, run /bump edit to add it"
        for item in wizard.children:
            item.disabled = True
        embed = wizard.build_embed()
        embed.set_footer(text=footer)
        await interaction.response.edit_message(embed=embed, view=wizard)
        wizard.stop()

        # Immediately send the first bump — same step /bump now runs — so the
        # server is on the network right after saving. _do_bump posts its own
        # result (and the 🔁 Bump prompt) via interaction.followup, which is
        # available now that the response above has been used. Best-effort:
        # a failure here never undoes the save.
        try:
            cog = interaction.client.get_cog("BumpCog")
            clone_id = _clone_id_of(interaction)
            config = await db.bump_get_guild_config(interaction.guild_id, clone_id=clone_id)
            listing = await db.bump_get_listing(interaction.guild_id, clone_id, "server")
            if cog and config and listing:
                await cog._do_bump(interaction, config, listing, clone_id)
            else:
                await interaction.followup.send(
                    "Saved ✅ — run `/bump now` to send your first bump.", ephemeral=True,
                )
        except Exception:
            logger.exception("[bumpsetup] first bump after save failed for guild %s", interaction.guild_id)


class BumpCancelButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        for item in self.wizard.children:
            item.disabled = True
        embed = self.wizard.build_embed()
        embed.set_footer(text="Cancelled — nothing was changed.")
        await interaction.response.edit_message(embed=embed, view=self.wizard)
        self.wizard.stop()


class BumpWizardView(discord.ui.View):
    def __init__(self, invoker_id: int, current: dict):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.channel_id = current.get("bump_channel_id")
        self.language = current.get("language") or "any"
        self.nsfw_opt_in = bool(current.get("nsfw_opt_in") or False)
        self.intensity_level = current.get("intensity_level") or 3
        self.created_channel_id = None

        self.add_item(BumpChannelSelect(self))
        self.add_item(BumpLanguageSelect(self))
        self.add_item(BumpIntensitySelect(self))
        self.add_item(BumpNsfwToggleButton(self))
        self.add_item(BumpCreateChannelButton(self))
        self.add_item(BumpFinishButton(self))
        self.add_item(BumpCancelButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran /bumpsetup can use this.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        def fmt_channel(cid):
            return f"<#{cid}>" if cid else "*not set*"

        lang_label = dict(LANGUAGE_OPTIONS).get(self.language, self.language)

        embed = discord.Embed(
            title="Bump network setup",
            description=(
                "**What is bumping?** Bumping is a free partnership network: when you bump, your server "
                "(or bot) is posted as an ad card in the bump channel of every other server on the network, "
                "and their servers and bots get posted in yours. More servers see you, and you discover "
                "partners in return.\n\n"
                "• Bump again whenever the cooldown ends — keeping a streak boosts your listing.\n"
                "• Receiving is part of the deal: your bump channel shows other servers' ads.\n"
                "• Filter by language and choose how often you want to receive bumps.\n\n"
                "**Setup:** pick a channel (or tap **Create #bump for me**), adjust the options, then hit "
                "**Save** — your first bump is sent automatically."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Bump channel", value=fmt_channel(self.channel_id), inline=True)
        embed.add_field(name="Language filter", value=lang_label, inline=True)
        embed.add_field(name="Frequency", value=INTENSITY_LABELS.get(self.intensity_level, str(self.intensity_level)), inline=True)
        embed.add_field(name="NSFW bumps", value="On" if self.nsfw_opt_in else "Off", inline=True)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class BumpSetupCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="bumpsetup", description="Set up this server's bump network settings")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bumpsetup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        current = await db.bump_get_guild_config(interaction.guild_id, clone_id=clone_id) or {}
        wizard = BumpWizardView(interaction.user.id, current)
        await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BumpSetupCog(bot))
