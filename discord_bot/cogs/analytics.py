# path: discord_bot/cogs/analytics.py

"""
Server analytics — a snapshot of the guild's size/activity plus concrete
suggestions for growing membership, using data already tracked elsewhere
in this bot (discord_xp for activity, discord_guilds for join history,
the live guild object for current counts).

Growth suggestions point at /bump (modules/bump.py's cross-server listing
network) since that's this bot's actual member-acquisition mechanism —
NOT discord_bot/cogs/discover.py, which is unrelated anime content
discovery despite the similar-sounding name.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db

logger = logging.getLogger(__name__)


def _require_manage_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return bool(interaction.permissions.manage_guild)


def _clone_id_of(bot) -> int | None:
    return getattr(bot, "clone_id", None)


GROWTH_TIPS = [
    ("📣", "Bump this server", "/bump now",
     "Posts your server to every other opted-in server on the bump network — the fastest zero-cost way to get in front of new people."),
    ("⏱️", "Set up bump reminders", "/bumpsetup",
     "Picks (or creates) a channel and reminds staff when the bump cooldown resets, so bumping actually happens on a schedule instead of being forgotten."),
    ("🔗", "Post an invite link where people already are", None,
     "Your server's socials, a linked community, or a bio link. A standing invite gets far more traffic than a one-off post."),
    ("🎉", "Run a giveaway", "/giveaway start",
     "Giveaways reliably spike short-term joins, especially when shared outside the server."),
    ("👋", "Turn on welcome messages", "/welcome setup",
     "New joiners who get greeted are noticeably more likely to stick around and become active members instead of silently leaving."),
]


class AnalyticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="serveranalytics", description="Snapshot of this server's size and activity, plus growth suggestions")
    async def serveranalytics(self, interaction: discord.Interaction):
        if not _require_manage_guild(interaction):
            await interaction.response.send_message("You need **Manage Server** to run this.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        clone_id = _clone_id_of(self.bot)

        try:
            active_7d = await db.count_active_members(guild.id, days=7, clone_id=clone_id)
        except Exception as e:
            logger.error(f"[v0] serveranalytics active-member count failed for guild {guild.id}: {e}")
            active_7d = None

        text_channels = sum(1 for c in guild.channels if isinstance(c, discord.TextChannel))
        voice_channels = sum(1 for c in guild.channels if isinstance(c, discord.VoiceChannel))
        role_count = max(len(guild.roles) - 1, 0)  # exclude @everyone

        embed = discord.Embed(
            title=f"📊 Analytics — {guild.name}",
            color=discord.Color.blurple(),
        )
        member_count = guild.member_count or len(guild.members) or None
        embed.add_field(
            name="Members",
            value=f"{member_count:,}" if member_count else "N/A",
            inline=True,
        )
        embed.add_field(
            name="Active (7d)",
            value=f"{active_7d:,}" if active_7d is not None else "N/A",
            inline=True,
        )
        embed.add_field(name="Server age", value=self._age_str(guild.created_at), inline=True)
        embed.add_field(name="Text channels", value=str(text_channels), inline=True)
        embed.add_field(name="Voice channels", value=str(voice_channels), inline=True)
        embed.add_field(name="Roles", value=str(role_count), inline=True)

        if active_7d is not None and member_count:
            pct = (active_7d / member_count) * 100
            embed.add_field(
                name="Note",
                value=f"About {pct:.0f}% of members have been active in the last 7 days. "
                      f"\"Active\" here means they've sent at least one XP-earning message.",
                inline=False,
            )

        tips_text = "\n\n".join(
            f"{emoji} **{name}**" + (f" — `{cmd}`" if cmd else "") + f"\n{blurb}"
            for emoji, name, cmd, blurb in GROWTH_TIPS
        )
        embed.add_field(name="Where to get more members", value=tips_text, inline=False)
        embed.set_footer(text="Activity numbers are a rough proxy (XP-cooldown-based), not exact message counts.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @staticmethod
    def _age_str(created_at) -> str:
        delta = discord.utils.utcnow() - created_at
        days = delta.days
        if days < 30:
            return f"{days}d"
        if days < 365:
            return f"{days // 30}mo"
        return f"{days // 365}y {(days % 365) // 30}mo"


async def setup(bot):
    await bot.add_cog(AnalyticsCog(bot))
