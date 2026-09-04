# path: discord_bot/cogs/quickstart.py

"""
Quick-start DM — sent to the server owner when the bot joins a new guild,
pointing them at a handful of ready-to-use setup commands.

Deliberately restrained so this never turns into spam:
  - Sent ONCE on join (best-effort — a closed-DM owner is skipped silently,
    same as bot.py's existing _alert_owners_of_join).
  - Exactly ONE follow-up, days later (QUICKSTART_FOLLOWUP_DAYS), and only
    if the guild genuinely hasn't configured anything yet (checked against
    welcome/automod/starboard/ticket config — see _guild_has_any_setup).
  - After that follow-up fires (or is skipped because they did set
    something up), discord_quickstart_dm.followup_sent_at /
    followup_skipped is set and this guild is never looked at again by the
    daily loop. No recurring nagging, ever.

This is separate from welcome.py's own nudge (which is specifically about
turning on welcome cards) — this one is a broader "here's what this bot can
do" pointer covering several unrelated features in one message.
"""

import logging

import discord
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs.setup_channels import scan_missing_channels, build_suggestions_embed, SetupSuggestView

logger = logging.getLogger(__name__)

QUICKSTART_FOLLOWUP_DAYS = 3

# Kept short on purpose — a DM with 10 bullet points reads as a wall of
# text and gets skimmed or ignored. These five cover the most broadly
# useful, lowest-effort-to-set-up features; everything else is still
# there and discoverable via /help.
QUICKSTART_ITEMS = [
    ("👋", "Welcome messages", "/welcome setup", "Greet new members automatically in a channel of your choice."),
    ("🛡️", "Auto-moderation", "/automod setup", "Filter spam, invite links, and mass-mention raids."),
    ("🎭", "Self-roles", "/role setup", "Bulk-create popular roles (Announcements, Giveaways, etc.) and post a self-assign panel in one wizard."),
    ("📈", "Leveling / XP", "/leveling setup", "Reward active members with levels and roles over time."),
    ("⬇️", "Media downloads", "/download", "Works right away, no setup — grab audio/video from a link."),
    ("📥", "Downloadhub", "/setup downloadhub", "Auto-creates a channel where members submit/upload music & video and play it in voice."),
    ("📊", "Server analytics", "/serveranalytics", "See member/activity stats and where to find more members."),
    ("📣", "Bump network", "/bumpsetup", "List your server for growth — I can even create the channel for you."),
]


class QuickstartCog(commands.Cog):
    # Exposed as a class attribute (not just the module-level name above)
    # because bot.py's _send_combined_owner_join_dm reads it off the cog
    # instance via self.get_cog("QuickstartCog").QUICKSTART_ITEMS — a bare
    # module-level global isn't reachable that way and raises AttributeError,
    # which bot.py's broad except Exception then swallows silently, dropping
    # the entire quickstart section (blurbs + "Turn on" buttons + pagination)
    # from the combined join DM with no visible error.
    QUICKSTART_ITEMS = QUICKSTART_ITEMS

    def __init__(self, bot):
        self.bot = bot
        self._followup_check.start()

    def cog_unload(self):
        self._followup_check.cancel()

    # ---- initial DM, fired from bot.py's on_guild_join -----------------

    async def send_initial_dm(self, guild: discord.Guild):
        """Deprecated standalone sender. The quickstart items are now a
        section inside bot.py's single combined on-join DM
        (_send_combined_owner_join_dm), which reads QUICKSTART_ITEMS and
        marks discord_quickstart_dm sent itself — so this no longer sends
        anything on its own. Kept only in case anything else still calls
        it directly."""
        return

    async def send_channel_suggestions_followup(self, guild: discord.Guild, owner: discord.abc.User = None):
        """The one on-join DM that still has to be separate from the
        combined embed, since it carries real buttons. Uses the live scan,
        not a canned list — a guild that already has e.g. a #welcome
        channel won't be asked again. Best-effort, never raises."""
        try:
            if owner is None:
                owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
            if owner is None:
                return
            clone_id = getattr(self.bot, "clone_id", None)
            missing = await scan_missing_channels(guild, clone_id)
            if missing:
                suggest_embed = build_suggestions_embed(guild, missing)
                view = SetupSuggestView(guild.id, missing)
                await owner.send(
                    content="Want me to set up a few channels for you too? One tap each, or all at once:",
                    embed=suggest_embed, view=view,
                )
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.info(f"[v0] Channel-suggestions follow-up skipped for guild {guild.id}: {e}")
        except Exception as e:
            logger.error(f"[v0] Channel-suggestions follow-up failed for guild {guild.id}: {e}")

    def _build_embed(self, guild: discord.Guild, intro: str) -> discord.Embed:
        embed = discord.Embed(
            title="🚀 Quick start",
            description=intro,
            color=discord.Color.blurple(),
        )
        for emoji, name, command, blurb in QUICKSTART_ITEMS:
            embed.add_field(name=f"{emoji} {name}", value=f"`{command}`\n{blurb}", inline=False)
        embed.set_footer(text="Run /help anytime for the full command list. This is the only reminder you'll get.")
        return embed

    # ---- one-time follow-up, only if nothing's been configured ---------

    @tasks.loop(hours=24)
    async def _followup_check(self):
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            guild_ids = await db.list_quickstart_followup_candidates(clone_id, days=QUICKSTART_FOLLOWUP_DAYS)
        except Exception as e:
            logger.error(f"[v0] Quickstart follow-up query failed: {e}")
            return

        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                # Bot isn't in this guild anymore (or it hasn't been
                # cached yet) — leave it for a later cycle rather than
                # guessing; on_guild_remove elsewhere handles true departures.
                continue
            try:
                if await self._guild_has_any_setup(guild.id, clone_id):
                    await db.mark_quickstart_followup_skipped(guild.id, clone_id)
                    continue
                owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
                if owner:
                    embed = self._build_embed(
                        guild,
                        intro=f"Quick nudge — **{guild.name}** hasn't set up any of these yet. Totally optional, just flagging in case it got buried:",
                    )
                    await owner.send(embed=embed)
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.info(f"[v0] Quickstart follow-up skipped for guild {guild_id}: {e}")
            except Exception as e:
                logger.error(f"[v0] Quickstart follow-up failed for guild {guild_id}: {e}")
            finally:
                # Marked sent regardless of DM success — this is a single
                # best-effort attempt, never retried, so a closed-DM owner
                # doesn't cause the loop to keep circling back to them.
                await db.mark_quickstart_followup_sent(guild_id, clone_id)

    @_followup_check.before_loop
    async def _before_followup_check(self):
        await self.bot.wait_until_ready()

    async def _guild_has_any_setup(self, guild_id: int, clone_id) -> bool:
        """True if the guild has turned on ANY of the suggested features —
        used to skip the follow-up for guilds that already found their way
        to setup on their own (e.g. via /help) without needing the nudge."""
        try:
            welcome = await db.get_welcome_config(guild_id, clone_id)
            if welcome.get("enabled"):
                return True
        except Exception:
            pass
        try:
            automod = await db.get_automod_config(guild_id, clone_id)
            if automod.get("enabled"):
                return True
        except Exception:
            pass
        try:
            starboard = await db.get_starboard_config(guild_id, clone_id)
            if starboard.get("channel_id"):
                return True
        except Exception:
            pass
        try:
            ticket = await db.get_ticket_config(guild_id, clone_id)
            if ticket.get("panel_channel_id"):
                return True
        except Exception:
            pass
        try:
            downloadhub = await db.get_download_config(guild_id, clone_id)
            if downloadhub.get("channel_id"):
                return True
        except Exception:
            pass
        return False


async def setup(bot):
    await bot.add_cog(QuickstartCog(bot))
