"""
Public post for the owner-panel referral giveaway (see modules/referral_giveaway.py
and discord_bot/cogs/_views_admin_panel_referral.py).

The owner creates/edits/ends the giveaway from /admin; this module only handles
what everybody else sees: an embed in a channel (description, how to enter,
prize, winners, ends, live top referrers), two persistent buttons ("My entries",
"Leaderboard"), a refresher that keeps the embed's numbers current, and the
winners announcement. It never changes giveaway data.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands, tasks

from modules import referral_giveaway as rg

logger = logging.getLogger(__name__)

REFRESH_MINUTES = 5
TOP_SHOWN = 5


def _owners() -> set:
    try:
        from config import DISCORD_CLONE_ADMIN_IDS, DISCORD_OWNER_BROADCAST_IDS
        return set(DISCORD_CLONE_ADMIN_IDS) | set(DISCORD_OWNER_BROADCAST_IDS)
    except Exception:
        return set()


def _ts(dt: datetime, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def _safe(text: Optional[str]) -> str:
    return discord.utils.escape_mentions(text or "")


def build_embed(g: dict, top: list, total: int) -> discord.Embed:
    ended = g["status"] == "ended"
    prize = _safe(g["prize"])
    if g.get("prize_kind") == "role" and g.get("role_id"):
        prize += f" (<@&{g['role_id']}> role)"
    desc = _safe(g.get("description")).strip()
    parts = []
    if desc:
        parts.append(desc)
    if not ended:
        parts.append(
            "**How to enter**\n"
            "1️⃣ Press **Get my code** below to get your personal code.\n"
            "2️⃣ Share it. Every new person who presses **Enter a code** and types yours counts as **1 entry** for you.\n"
            "3️⃣ The people with the most referrals when time runs out win. You can't win by using your own code.\n"
            "-# Got a code from a friend? Press **Enter a code**. Each person can only use one code, ever."
        )
    parts.append(
        f"**Prize:** {prize}\n"
        f"**Winners:** {g['winner_count']}\n"
        + (f"**Ended:** {_ts(g['ended_at'] or g['ends_at'])}\n" if ended else f"**Ends:** {_ts(g['ends_at'])} ({_ts(g['ends_at'], 'f')})\n")
        + f"**Referrals so far:** {total}"
    )
    if ended:
        if g.get("winners"):
            parts.append("**🏆 Winners**\n" + "\n".join(
                f"**{n}.** <@{w['user_id']}> — {w['count']} referral(s)" for n, w in enumerate(g["winners"], 1)))
        else:
            parts.append("Ended with no winners (nobody referred anyone).")
    elif top:
        parts.append(f"**Top referrers**\n" + "\n".join(
            f"**{n}.** <@{r['user_id']}> — {r['count']}" for n, r in enumerate(top[:TOP_SHOWN], 1)))
    else:
        parts.append("**Top referrers**\nNo referrals yet. Be the first!")
    embed = discord.Embed(title=f"🎁 {_safe(g['title'])}", description="\n\n".join(parts)[:4000],
                          color=discord.Color.dark_grey() if ended else discord.Color.gold())
    embed.set_footer(text=f"Referral giveaway #{g['id']}")
    return embed


async def render(g: dict) -> discord.Embed:
    try:
        top = [] if g["status"] == "ended" else await rg.standings(g, _owners(), limit=TOP_SHOWN)
        total = await rg.total_entries(g)
    except Exception:
        logger.exception("[referral-giveaway-post] couldn't load numbers for #%s", g.get("id"))
        top, total = [], 0
    return build_embed(g, top, total)


_USE_MESSAGES = {
    "applied": "✅ Applied! That referral is locked in. Thanks for joining!",
    "already_set": "You've already used a referral code before. It only works once per person, ever.",
    "self": "You can't use your own referral code.",
    "not_found": "That code doesn't match anyone. Double-check it and try again.",
    "error": "❌ Something went wrong. Try again.",
}


class UseCodeModal(discord.ui.Modal, title="Enter a referral code"):
    def __init__(self, message_id: int):
        super().__init__(timeout=300)
        self.message_id = message_id
        self.code = discord.ui.TextInput(label="Code from your friend", max_length=32, placeholder="e.g. AB12CD34")
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        g = await rg.get_by_message(self.message_id)
        if g is None or g["status"] != "active":
            await interaction.followup.send("This giveaway has ended, so codes can't be entered here any more.",
                                            ephemeral=True)
            return
        from modules.referrals import use_referral_code
        result = await use_referral_code(interaction.user.id, self.code.value.strip())
        await interaction.followup.send(_USE_MESSAGES.get(result.get("reason"), _USE_MESSAGES["error"]),
                                        ephemeral=True)
        if result.get("ok"):
            await refresh_post(interaction.client, g)


class PostView(discord.ui.View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        for item in self.children:
            item.disabled = disabled

    @discord.ui.button(label="Get my code", style=discord.ButtonStyle.success, emoji="🔗", custom_id="refgw:code")
    async def code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        g = await rg.get_by_message(interaction.message.id)
        if g is None or g["status"] != "active":
            await interaction.followup.send("This giveaway has ended.", ephemeral=True)
            return
        if interaction.user.id in _owners():
            await interaction.followup.send("The host can't win their own giveaway.", ephemeral=True)
            return
        from modules.referrals import get_or_create_referral_code
        code = await get_or_create_referral_code(interaction.user.id)
        if not code:
            await interaction.followup.send("❌ Couldn't get a code right now. Try again.", ephemeral=True)
            return
        await interaction.followup.send(
            f"🔗 Your referral code: `{code}`\n"
            f"Share it. Every new person who presses **Enter a code** on the giveaway post and types it "
            f"counts as 1 entry for you.", ephemeral=True)

    @discord.ui.button(label="Enter a code", style=discord.ButtonStyle.primary, emoji="⌨️", custom_id="refgw:use")
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = await rg.get_by_message(interaction.message.id)
        if g is None or g["status"] != "active":
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        await interaction.response.send_modal(UseCodeModal(interaction.message.id))

    @discord.ui.button(label="My entries", style=discord.ButtonStyle.secondary, emoji="🎟️", custom_id="refgw:me")
    async def me(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        g = await rg.get_by_message(interaction.message.id)
        if g is None:
            await interaction.followup.send("This giveaway isn't available any more.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in _owners():
            await interaction.followup.send("The host can't win their own giveaway.", ephemeral=True)
            return
        stats = await rg.entry_stats(g, uid, _owners())
        if g["status"] == "ended":
            won = any(w["user_id"] == uid for w in g.get("winners", []))
            await interaction.followup.send(
                f"This giveaway has ended. You referred **{stats['count']}** (" + ("🏆 you won!" if won else "not a winner") + ").",
                ephemeral=True)
            return
        if stats["count"]:
            await interaction.followup.send(
                f"🎟️ You have **{stats['count']}** referral(s) and you're **#{stats['rank']}** of "
                f"{stats['total_referrers']}. Press **Get my code** and share it to climb.", ephemeral=True)
        else:
            await interaction.followup.send(
                "🎟️ You have **0** referrals so far. Press **Get my code**, share it, and every new person "
                "who presses **Enter a code** and types it counts for you.", ephemeral=True)

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.secondary, emoji="🏆", custom_id="refgw:top")
    async def top(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        g = await rg.get_by_message(interaction.message.id)
        if g is None:
            await interaction.followup.send("This giveaway isn't available any more.", ephemeral=True)
            return
        table = (await rg.standings(g, _owners(), limit=rg.STANDINGS_LIMIT)) if g["status"] == "active" else [
            {"user_id": w["user_id"], "count": w["count"]} for w in g.get("winners", [])]
        if not table:
            await interaction.followup.send("No referrals yet.", ephemeral=True)
            return
        await interaction.followup.send(
            "**Leaderboard**\n" + "\n".join(f"**{n}.** <@{r['user_id']}> — {r['count']}" for n, r in enumerate(table, 1)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _fetch_message(bot, g: dict) -> Optional[discord.Message]:
    if not g.get("channel_id") or not g.get("message_id"):
        return None
    ch = bot.get_channel(g["channel_id"])
    if ch is None:
        try:
            ch = await bot.fetch_channel(g["channel_id"])
        except discord.HTTPException:
            return None
    try:
        return await ch.fetch_message(g["message_id"])
    except discord.NotFound:
        return None
    except discord.HTTPException:
        return None


async def post_giveaway(bot, g: dict, channel: discord.abc.Messageable) -> Optional[discord.Message]:
    """Send (or move) the public post. Returns the message, or None if sending failed."""
    try:
        msg = await channel.send(embed=await render(g), view=PostView(),
                                 allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        logger.warning("[referral-giveaway-post] couldn't post #%s: %s", g.get("id"), e)
        return None
    old = await _fetch_message(bot, g)
    await rg.set_post(g["id"], msg.channel.id, msg.id)
    if old is not None and old.id != msg.id:
        try:
            await old.delete()
        except discord.HTTPException:
            pass
    return msg


async def refresh_post(bot, g: dict) -> bool:
    """Re-render the public post from the current row. Safe to call any time."""
    msg = await _fetch_message(bot, g)
    if msg is None:
        return False
    try:
        await msg.edit(embed=await render(g), view=PostView(disabled=g["status"] == "ended"),
                       allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException as e:
        logger.warning("[referral-giveaway-post] couldn't refresh #%s: %s", g.get("id"), e)
        return False


async def delete_post(bot, g: dict) -> bool:
    """Take the public post down. Never raises; False if it was already gone or couldn't be deleted."""
    msg = await _fetch_message(bot, g)
    if msg is None:
        return False
    try:
        await msg.delete()
        return True
    except discord.HTTPException as e:
        logger.warning("[referral-giveaway-post] couldn't delete post for #%s: %s", g.get("id"), e)
        return False


async def announce_winners(bot, g: dict) -> None:
    """Refresh the post into its 'ended' form and reply under it with the winners."""
    await refresh_post(bot, g)
    msg = await _fetch_message(bot, g)
    if msg is None:
        return
    if g.get("winners"):
        text = "🏆 **Giveaway ended!** Congratulations " + ", ".join(f"<@{w['user_id']}>" for w in g["winners"]) + \
               f"\nPrize: {_safe(g['prize'])}"
        mentions = discord.AllowedMentions(users=True)
    else:
        text = "🏁 Giveaway ended with no winners (nobody referred anyone)."
        mentions = discord.AllowedMentions.none()
    try:
        await msg.reply(text, allowed_mentions=mentions)
    except discord.HTTPException as e:
        logger.warning("[referral-giveaway-post] couldn't announce #%s: %s", g.get("id"), e)


class ReferralGiveawayPostCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresher.start()

    async def cog_load(self):
        self.bot.add_view(PostView())

    def cog_unload(self):
        self._refresher.cancel()

    @tasks.loop(minutes=REFRESH_MINUTES)
    async def _refresher(self):
        if getattr(self.bot, "clone_id", None):      # only the bot that posted can edit its messages
            return
        try:
            for g in await rg.list_posted(active_only=True):
                await refresh_post(self.bot, g)
        except Exception:
            logger.exception("[referral-giveaway-post] refresher failed")

    @_refresher.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ReferralGiveawayPostCog(bot))
