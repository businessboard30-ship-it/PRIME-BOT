# path: discord_bot/cogs/ship.py

"""
"Shipping" feature — every poller tick the bot may randomly pick two
currently-active members in a guild and post a 💘 prompt suggesting
they're a couple, with Accept/Reject buttons.

"Currently active" = in a voice channel right now, OR sent a message in
the configured ship channel within ACTIVE_WINDOW_MINUTES (tracked
in-memory via on_message, no DB table needed for that part — it's a
short rolling window, not history).

Flow:
  1. _poller (every POLL_INTERVAL_SECONDS) rolls, per guild, whether to
     fire based on discord_ship_config (check_interval_minutes +
     chance_percent), same cooldown-then-roll shape as roast.py's
     random-chance trigger.
  2. On fire: picks 2 distinct active members, posts an embed (image from
     a curated SFW URL list — NOT user-supplied media, NOT scraped clips,
     see SHIP_IMAGE_URLS) with a ShipPromptView (Accept / Reject).
  3. Either shipped member can click a button. Accept -> confirmation
     message, buttons disabled. Reject -> "my bad" message, buttons
     disabled. Unused after SHIP_PROMPT_TIMEOUT_SECONDS -> view times out,
     buttons disabled quietly.
  4. Row logged in discord_ship_history purely so back-to-back polls
     don't need to re-derive anything — no leaderboard/stats requested.
"""

import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
ACTIVE_WINDOW_MINUTES = 10  # how recently someone must have texted to count as "active"
SHIP_PROMPT_TIMEOUT_SECONDS = 600
SHIPS_PER_DAY_CAP = 3
DEFAULT_CHECK_INTERVAL_MINUTES = (24 * 60) // SHIPS_PER_DAY_CAP  # spread the 3 evenly across a day
DEFAULT_CHANCE_PERCENT = 100  # always fire once the interval + daily cap allow it

# Curated SFW image/GIF URLs for the ship prompt embed. Replace/extend
# this list with your own picks — deliberately NOT pulling from a live
# search API or embedding arbitrary user-supplied clips, so there's no
# copyright/ToS exposure and no risk of an unmoderated result slipping
# into the embed. Keep every entry safe-for-work.
SHIP_IMAGE_URLS = [
    "https://media.tenor.com/xmYz4zLmKl8AAAA1/taiga-aisaka-ryuuji-takasu.webp",
    "https://media.tenor.com/RxOLELh65TEAAAAM/kiss-anime-the-villainess-is-adored-by-the-prince-of-the-neighbor-kingdom.gif",
    "https://media.tenor.com/Gco2sDG_hFsAAAAM/%D1%86%D0%B8%D1%82%D1%80%D1%83%D1%81.gif",
    "https://media.tenor.com/EsKyXpC2wPUAAAAM/kiss-josee.gif",
]

REJECT_LINE = "My bad, someone ain't lucky today. 💔"


def _is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id in DISCORD_CLONE_ADMIN_IDS


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


class ShipPromptView(discord.ui.View):
    """Posted in the target channel. Only the two shipped members can
    respond — anyone else clicking gets a quiet ephemeral no-op."""

    def __init__(self, user_a: discord.Member, user_b: discord.Member):
        super().__init__(timeout=SHIP_PROMPT_TIMEOUT_SECONDS)
        self.user_a = user_a
        self.user_b = user_b
        self.responded = False
        self.message: discord.Message | None = None

    def _is_participant(self, user: discord.abc.User) -> bool:
        return user.id in (self.user_a.id, self.user_b.id)

    async def _disable(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self._disable()

    @discord.ui.button(label="Accept 💘", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_participant(interaction.user):
            await interaction.response.send_message("This one's not for you. 👀", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.responded = True
        await self._disable()
        await interaction.response.send_message(
            f"💘 It's official — {self.user_a.mention} x {self.user_b.mention} confirmed!"
        )

    @discord.ui.button(label="Reject 💔", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_participant(interaction.user):
            await interaction.response.send_message("This one's not for you. 👀", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.responded = True
        await self._disable()
        await interaction.response.send_message(REJECT_LINE)


class ShipCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {user_id: last_text_activity_at}, in-memory only,
        # trimmed lazily on read — this is a short rolling window, not a
        # durable log, so it doesn't need a table.
        self._recent_text_activity: dict[int, dict[int, datetime]] = {}
        self._poller.start()

    def cog_unload(self):
        self._poller.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        guild_activity = self._recent_text_activity.setdefault(message.guild.id, {})
        guild_activity[message.author.id] = datetime.now(timezone.utc)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in list(self.bot.guilds):
            try:
                await self._send_onboarding_dm_if_needed(guild)
            except Exception:
                logger.exception(f"[ship] onboarding DM check failed for guild={guild.id}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self._send_onboarding_dm_if_needed(guild)
        except Exception:
            logger.exception(f"[ship] onboarding DM check failed for guild={guild.id}")

    async def _send_onboarding_dm_if_needed(self, guild: discord.Guild):
        """Deprecated standalone sender. The onboarding blurb is now folded
        into bot.py's single combined join DM via build_join_notice_field()
        below, so this no longer sends anything itself."""
        return

    async def build_join_notice_field(self, guild: discord.Guild):
        """Returns (title, body) for the combined on-join DM, or None if
        this notice has already been sent for this guild. One-time-ever,
        guarded by onboarding_dm_sent in the DB (not memory) so it survives
        restarts and never fires twice."""
        clone_id = _clone_id_of(self.bot)
        config = await self.get_config(guild.id, clone_id)
        if config["onboarding_dm_sent"]:
            return None

        # Claim the row before returning content, so a slow send or a race
        # on startup can't result in this being included twice — worst
        # case we mark it sent and the combined DM happens to fail.
        await db.execute(
            """
            INSERT INTO discord_ship_config (guild_id, clone_id, channel_id, check_interval_minutes, chance_percent, enabled, onboarding_dm_sent)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET onboarding_dm_sent = TRUE
            """,
            guild.id, clone_id, config["channel_id"], config["check_interval_minutes"],
            config["chance_percent"], config["enabled"],
        )

        return (
            "💘 New feature: Ship",
            "I can randomly pair up two currently-active members with a fun kiss-card + "
            "Accept/Reject buttons, up to 3 times a day. It's off by default — run "
            "`/setup shipconfig` in the server and set a channel + enabled:True to turn it on.",
        )

    # ---------- config ----------

    async def get_config(self, guild_id: int, clone_id):
        row = await db.fetchrow(
            "SELECT * FROM discord_ship_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id,
        )
        if row:
            return row
        return {
            "guild_id": guild_id,
            "clone_id": clone_id,
            "channel_id": None,
            "check_interval_minutes": DEFAULT_CHECK_INTERVAL_MINUTES,
            "chance_percent": DEFAULT_CHANCE_PERCENT,
            "enabled": False,
            "onboarding_dm_sent": False,
        }

    async def configure(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                         check_interval_minutes: int = None, chance_percent: int = None, enabled: bool = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        clone_id = _clone_id_of(self.bot)
        current = await self.get_config(interaction.guild.id, clone_id)
        new_channel_id = channel.id if channel is not None else current["channel_id"]
        new_interval = check_interval_minutes if check_interval_minutes is not None else current["check_interval_minutes"]
        new_chance = chance_percent if chance_percent is not None else current["chance_percent"]
        new_enabled = enabled if enabled is not None else current["enabled"]
        await db.execute(
            """
            INSERT INTO discord_ship_config (guild_id, clone_id, channel_id, check_interval_minutes, chance_percent, enabled)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET channel_id = $3, check_interval_minutes = $4, chance_percent = $5, enabled = $6
            """,
            interaction.guild.id, clone_id, new_channel_id, new_interval, new_chance, new_enabled,
        )
        channel_desc = f"<#{new_channel_id}>" if new_channel_id else "(not set — will skip until one is picked)"
        await interaction.followup.send(
            f"✅ Ship config updated — channel: {channel_desc}, check every {new_interval}m, "
            f"chance: {new_chance}%, enabled: {new_enabled}",
            ephemeral=True,
        )

    # ---------- active member pool ----------

    def _active_pool(self, guild: discord.Guild) -> list[discord.Member]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=ACTIVE_WINDOW_MINUTES)
        pool: dict[int, discord.Member] = {}

        for vc in guild.voice_channels:
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                continue
            for member in vc.members:
                if not member.bot:
                    pool[member.id] = member

        guild_activity = self._recent_text_activity.get(guild.id, {})
        for user_id, last_at in list(guild_activity.items()):
            if last_at < cutoff:
                del guild_activity[user_id]
                continue
            member = guild.get_member(user_id)
            if member and not member.bot:
                pool[member.id] = member

        return list(pool.values())

    # ---------- poller ----------

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self):
        for guild in list(self.bot.guilds):
            try:
                await self._check_guild(guild)
            except Exception:
                logger.exception(f"[ship] poller failed for guild={guild.id}")

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    async def _check_guild(self, guild: discord.Guild):
        clone_id = _clone_id_of(self.bot)
        config = await self.get_config(guild.id, clone_id)
        if not config["enabled"] or not config["channel_id"]:
            return

        last_ship = await db.fetchrow(
            "SELECT created_at FROM discord_ship_history WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "ORDER BY created_at DESC LIMIT 1",
            guild.id, clone_id,
        )
        now = datetime.now(timezone.utc)
        if last_ship:
            minutes_since = (now - last_ship["created_at"]).total_seconds() / 60
            if minutes_since < config["check_interval_minutes"]:
                return

        today_count = await db.fetchval(
            "SELECT COUNT(*) FROM discord_ship_history WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "AND created_at >= date_trunc('day', NOW())",
            guild.id, clone_id,
        )
        if today_count >= SHIPS_PER_DAY_CAP:
            return

        if random.randint(1, 100) > config["chance_percent"]:
            return

        pool = self._active_pool(guild)
        if len(pool) < 2:
            return

        channel = guild.get_channel(config["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        user_a, user_b = random.sample(pool, 2)
        await self._send_ship(guild, channel, user_a, user_b, clone_id)

    async def _send_ship(self, guild: discord.Guild, channel: discord.TextChannel,
                          user_a: discord.Member, user_b: discord.Member, clone_id):
        embed = discord.Embed(
            title="💘 Shipping Alert!",
            description=f"Maybe {user_a.mention} and {user_b.mention} are... kissing? 👀",
            color=discord.Color.pink(),
        )
        if SHIP_IMAGE_URLS:
            embed.set_image(url=random.choice(SHIP_IMAGE_URLS))

        view = ShipPromptView(user_a, user_b)
        message = await channel.send(embed=embed, view=view)
        view.message = message

        await db.execute(
            """
            INSERT INTO discord_ship_history (guild_id, clone_id, user_a_id, user_b_id)
            VALUES ($1, $2, $3, $4)
            """,
            guild.id, clone_id, user_a.id, user_b.id,
        )
        logger.info(f"[ship] posted guild={guild.id} pair=({user_a.id},{user_b.id})")

    # ---------- admin manual trigger ----------

    async def manual_trigger(self, interaction: discord.Interaction):
        if not _is_admin_member(interaction.user):
            await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
            return
        clone_id = _clone_id_of(self.bot)
        config = await self.get_config(interaction.guild.id, clone_id)
        channel_id = config["channel_id"] or interaction.channel.id
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("⚠️ No valid ship channel configured.", ephemeral=True)
            return
        pool = self._active_pool(interaction.guild)
        if len(pool) < 2:
            await interaction.response.send_message(
                "⚠️ Not enough currently-active members (voice or recent chat) to ship right now.",
                ephemeral=True,
            )
            return
        user_a, user_b = random.sample(pool, 2)
        await self._send_ship(interaction.guild, channel, user_a, user_b, clone_id)
        await interaction.response.send_message(f"✅ Shipped in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShipCog(bot))
