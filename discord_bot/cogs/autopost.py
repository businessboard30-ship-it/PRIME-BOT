"""
Autopost — periodic self-promo posts about the bot's own features.

Two separate surfaces, on purpose (see database.py's discord_autopost_config
/ discord_autopost_content comments for the schema-level version of this):

- `/autopost setup|disable|status` — a per-guild ON/Off switch + channel +
  interval. This is all a server admin controls. There is no per-category
  picker here: every guild that has autopost on cycles through the SAME
  rotating content library, just like a single bot's "did you know" tips
  rotation looks the same everywhere it's installed.
- `/autopostcontent add|list|remove` — bot-owner-only (DISCORD_CLONE_ADMIN_IDS),
  manages that shared content library: one row per {category, title, body,
  example_command}. Different post each cycle, different command/feature
  highlighted, different guidelines/usage each time — so it doesn't repeat
  the same content back-to-back. New commands get their own row here rather
  than a hardcoded string buried in this file.

Runs on every process (main bot AND every clone) — each queries only its own
clone_id's due configs (see get_due_discord_autoposts), so there's no
duplicate-post risk the way a user-scoped, non-clone-aware loop would have
(see crypto_alerts.py's comment on that exact failure mode).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from database import db

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = 15  # how often the loop wakes up to check for due guilds
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 720  # 30 days — sanity ceiling, not a hard product requirement


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _require_manage_guild(interaction: discord.Interaction) -> bool:
    # interaction.permissions (not interaction.user.guild_permissions) — the
    # latter needs a real discord.Member, which Discord doesn't give us when
    # this app is invoked via a user-install context, even inside a guild
    # channel. interaction.permissions is always populated correctly there.
    if interaction.guild is None:
        return False
    return bool(interaction.permissions.manage_guild)


def _is_bot_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _content_embed(entry: dict) -> discord.Embed:
    embed = discord.Embed(
        color=discord.Color.blurple(),
        title=f"💡 {entry['title']}",
        description=entry["body"],
    )
    if entry.get("example_command"):
        embed.add_field(name="Try it", value=f"`{entry['example_command']}`")
    embed.set_footer(text=entry["category"].replace("_", " ").title())
    return embed


class AutopostCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._post_loop.start()

    def cog_unload(self):
        self._post_loop.cancel()

    # ── /autopost (per-guild toggle) ────────────────────────────────────
    autopost = app_commands.guild_only()(
        app_commands.Group(name="autopost", description="Periodic bot self-promo posts in this server")
    )

    @autopost.command(name="setup", description="Turn on periodic bot feature posts in a channel")
    @app_commands.describe(channel="Where to post", interval="Hours between posts (min 1)")
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         interval: app_commands.Range[int, MIN_INTERVAL_HOURS, MAX_INTERVAL_HOURS]):
        await interaction.response.defer(ephemeral=True)
        if not _require_manage_guild(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                f"I need **Send Messages** and **Embed Links** permission in {channel.mention} first.",
                ephemeral=True,
            )
            return

        content_count = len(await db.list_discord_autopost_content())
        if content_count == 0:
            await interaction.followup.send(
                "There's no autopost content configured yet — nothing to rotate through. Try again once some exists.",
                ephemeral=True,
            )
            return

        await db.set_discord_autopost(
            interaction.guild_id, _clone_id_of(interaction), channel.id, interval, interaction.user.id
        )
        await interaction.followup.send(
            f"✅ Autopost is on — I'll post in {channel.mention} every **{interval}h**.", ephemeral=True
        )

    @autopost.command(name="disable", description="Turn off periodic bot feature posts")
    async def disable_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_manage_guild(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        was_on = await db.disable_discord_autopost(interaction.guild_id, _clone_id_of(interaction))
        if was_on:
            await interaction.followup.send("✅ Autopost turned off.", ephemeral=True)
        else:
            await interaction.followup.send("Autopost wasn't set up in this server.", ephemeral=True)

    @autopost.command(name="status", description="Check this server's autopost configuration")
    async def status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = await db.get_discord_autopost(interaction.guild_id, _clone_id_of(interaction))
        if not cfg:
            await interaction.followup.send("Autopost isn't configured in this server yet — use `/autopost setup`.", ephemeral=True)
            return
        state = "🟢 On" if cfg["enabled"] else "🔴 Off"
        channel_mention = f"<#{cfg['channel_id']}>"
        last = f"<t:{int(cfg['last_posted_at'].timestamp())}:R>" if cfg["last_posted_at"] else "never"
        await interaction.followup.send(
            f"{state} · posting in {channel_mention} every **{cfg['interval_hours']}h** · last post: {last}",
            ephemeral=True,
        )

    # ── /autopostcontent (bot-owner-managed shared library) ────────────
    autopostcontent = app_commands.Group(name="autopostcontent", description="[Owner] Manage the shared autopost content library")

    @autopostcontent.command(name="add", description="[Owner] Add a post to the autopost rotation")
    @app_commands.describe(
        category="Short category tag, e.g. 'moderation'", title="Post title",
        body="Post body text", example_command="Optional example command, e.g. '/warn'"
    )
    async def content_add(self, interaction: discord.Interaction, category: str, title: str, body: str,
                           example_command: str = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_bot_owner(interaction.user.id):
            await interaction.followup.send("This is owner-only.", ephemeral=True)
            return
        content_id = await db.add_discord_autopost_content(category, title, body, example_command, interaction.user.id)
        await interaction.followup.send(f"✅ Added content #{content_id}.", ephemeral=True)

    @autopostcontent.command(name="list", description="[Owner] List the autopost content library")
    async def content_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_bot_owner(interaction.user.id):
            await interaction.followup.send("This is owner-only.", ephemeral=True)
            return
        entries = await db.list_discord_autopost_content()
        if not entries:
            await interaction.followup.send("No autopost content yet.", ephemeral=True)
            return
        lines = [f"`#{e['id']}` **[{e['category']}]** {e['title']}" for e in entries]
        embed = discord.Embed(color=discord.Color.blurple(), title="Autopost Content Library", description="\n".join(lines)[:4000])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @autopostcontent.command(name="remove", description="[Owner] Remove a post from the autopost rotation")
    @app_commands.describe(content_id="The # shown in /autopostcontent list")
    async def content_remove(self, interaction: discord.Interaction, content_id: int):
        await interaction.response.defer(ephemeral=True)
        if not _is_bot_owner(interaction.user.id):
            await interaction.followup.send("This is owner-only.", ephemeral=True)
            return
        ok = await db.remove_discord_autopost_content(content_id)
        if ok:
            await interaction.followup.send(f"✅ Removed #{content_id}.", ephemeral=True)
        else:
            await interaction.followup.send(f"No content #{content_id} found.", ephemeral=True)

    # ── background loop ─────────────────────────────────────────────────
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def _post_loop(self):
        try:
            content = await db.list_discord_autopost_content()
            if not content:
                return
            due = await db.get_due_discord_autoposts(getattr(self.bot, "clone_id", None))
        except Exception as e:
            logger.error(f"[autopost] Could not load due configs: {e}")
            return

        for cfg in due:
            channel = self.bot.get_channel(cfg["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(cfg["channel_id"])
                except discord.HTTPException:
                    # Channel deleted or bot no longer has access — advance the
                    # index anyway so a fixed config doesn't retry-loop forever,
                    # but leave `enabled` alone (guild admin can /autopost setup
                    # again once fixed rather than silently getting disabled).
                    next_index = (cfg["current_index"] + 1) % len(content)
                    try:
                        await db.advance_discord_autopost(cfg["guild_id"], cfg["clone_id"], next_index)
                    except Exception:
                        logger.exception(f"[autopost] Failed to advance index for guild {cfg['guild_id']} (missing channel)")
                    continue

            entry = content[cfg["current_index"] % len(content)]
            try:
                await channel.send(embed=_content_embed(entry))
            except discord.HTTPException as e:
                logger.warning(f"[autopost] Failed to post in guild {cfg['guild_id']} channel {cfg['channel_id']}: {e}")

            # Always try to advance, even if the send above failed — a channel
            # missing Send Messages permission should back off for a full
            # interval, not get retried (and re-logged) every 15 minutes.
            # This whole block is isolated per-guild: if advance itself throws
            # (DB hiccup), we log and move to the next guild rather than
            # aborting the batch — an uncaught exception here would silently
            # skip every remaining due guild in this tick AND leave this
            # guild's last_posted_at stale, causing a duplicate post next
            # successful cycle.
            next_index = (cfg["current_index"] + 1) % len(content)
            try:
                await db.advance_discord_autopost(cfg["guild_id"], cfg["clone_id"], next_index)
            except Exception:
                logger.exception(f"[autopost] Failed to advance autopost index for guild {cfg['guild_id']}")

    @_post_loop.before_loop
    async def _before_post_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AutopostCog(bot))
