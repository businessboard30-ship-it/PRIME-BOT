# path: discord_bot/cogs/_views_leaderboard_links.py

"""
Server-link submission + admin review flow for /leadership (see
leveling.py's `leadership` command). Shape:

1. A top-10 member with no discord_leader_links row yet gets DMed a
   prompt (LeaderLinkPromptView) with "Add link" / "Skip" buttons.
2. "Add link" opens LeaderLinkModal — on submit, the link is stored as
   'pending' (db.submit_leader_link) and a review card is posted into
   the guild's configured leveling announce channel (falls back to the
   guild's system channel, then silently gives up if neither exists —
   same fallback chain used elsewhere for other admin-notice posts).
3. The review card's Approve/Deny buttons are DynamicItems (survive
   restarts, same convention as bump.py's DynamicBumpApproveButton)
   gated on Manage Server in the guild the link belongs to — NOT bot
   owner, since this is a per-guild admin action, not a bot-wide one.

"Skip" just closes the prompt with no DB row — the member will be
re-prompted next time /leadership runs and they're still in the top 10
and still have no row (skip doesn't write a 'denied' row on purpose,
since skipping isn't the same as an admin rejecting a submitted link).
"""

import logging

import discord

from database import db

logger = logging.getLogger(__name__)


def _require_manage_guild_in(guild: "discord.Guild | None", user_id: int) -> bool:
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return bool(member and member.guild_permissions.manage_guild)


class LeaderLinkModal(discord.ui.Modal, title="Add your server's invite link"):
    invite_url = discord.ui.TextInput(
        label="Invite link", placeholder="https://discord.gg/yourserver", max_length=200
    )

    def __init__(self, guild_id: int, clone_id: int | None, guild_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.guild_name = guild_name

    async def on_submit(self, interaction: discord.Interaction):
        url = str(self.invite_url.value).strip()
        if not url.startswith("https://discord.gg/") and not url.startswith("https://discord.com/invite/"):
            await interaction.response.send_message(
                "That doesn't look like a Discord invite link — it should start with "
                "`https://discord.gg/` or `https://discord.com/invite/`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        link = await db.submit_leader_link(self.guild_id, interaction.user.id, url, clone_id=self.clone_id)
        await interaction.followup.send(
            "Submitted — an admin needs to approve it before it shows up on `/leadership`.", ephemeral=True
        )
        await _post_review_card(interaction.client, link, self.guild_name, interaction.user)


class LeaderLinkPromptView(discord.ui.View):
    """Sent as a DM. Not persistent (no custom_id survival needed) — if
    the bot restarts before the member responds, they'll just get
    re-prompted the next time /leadership runs, same as any other
    unanswered DM prompt in this codebase."""

    def __init__(self, guild_id: int, clone_id: int | None, guild_name: str):
        super().__init__(timeout=3600)
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.guild_name = guild_name

    @discord.ui.button(label="Add link", style=discord.ButtonStyle.success, emoji="🔗")
    async def add_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LeaderLinkModal(self.guild_id, self.clone_id, self.guild_name))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="No problem — you can always run `/leadership` again later to add it.", view=None)


async def _post_review_card(bot, link: dict, guild_name: str, submitter: discord.User) -> None:
    guild = bot.get_guild(link["guild_id"])
    if guild is None:
        return

    channel = None
    try:
        cfg = await db.get_leveling_config(link["guild_id"], clone_id=link.get("clone_id"))
        if cfg and cfg.get("announce_channel_id"):
            channel = guild.get_channel(cfg["announce_channel_id"])
    except Exception:
        logger.exception("Failed to load leveling config for leader-link review post (guild %s)", link["guild_id"])
    if channel is None:
        channel = guild.system_channel
    if channel is None:
        logger.warning("No channel available to post leader-link review card for guild %s — skipping", link["guild_id"])
        return

    embed = discord.Embed(
        title="🏆 Leaderboard link submitted",
        description=f"**{submitter.display_name}** wants to show their server's invite on `/leadership`.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Server", value=guild_name, inline=True)
    embed.add_field(name="Invite", value=link["invite_url"], inline=False)

    view = discord.ui.View(timeout=None)
    view.add_item(DynamicLeaderLinkApproveButton(link["id"]))
    view.add_item(DynamicLeaderLinkDenyButton(link["id"]))

    try:
        await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        logger.exception("Failed to post leader-link review card in guild %s", link["guild_id"])


class DynamicLeaderLinkApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaderlink:approve:(?P<link_id>\d+)",
):
    def __init__(self, link_id: int):
        self.link_id = link_id
        super().__init__(
            discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"leaderlink:approve:{link_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["link_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not _require_manage_guild_in(interaction.guild, interaction.user.id):
            await interaction.response.send_message("You need **Manage Server** to approve this.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            link = await db.review_leader_link(self.link_id, approve=True, reviewer_id=interaction.user.id)
        except Exception:
            logger.exception("review_leader_link(approve) failed for link %s", self.link_id)
            await interaction.followup.send("Something went wrong approving this — check the bot logs.", ephemeral=True)
            return
        label = "✅ Approved — it'll now show on `/leadership`." if link else "Already handled (link not found)."
        await interaction.edit_original_response(content=label, embed=None, view=None)


class DynamicLeaderLinkDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"leaderlink:deny:(?P<link_id>\d+)",
):
    def __init__(self, link_id: int):
        self.link_id = link_id
        super().__init__(
            discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌",
                               custom_id=f"leaderlink:deny:{link_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["link_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not _require_manage_guild_in(interaction.guild, interaction.user.id):
            await interaction.response.send_message("You need **Manage Server** to deny this.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            link = await db.review_leader_link(self.link_id, approve=False, reviewer_id=interaction.user.id)
        except Exception:
            logger.exception("review_leader_link(deny) failed for link %s", self.link_id)
            await interaction.followup.send("Something went wrong denying this — check the bot logs.", ephemeral=True)
            return
        label = "❌ Denied." if link else "Already handled (link not found)."
        await interaction.edit_original_response(content=label, embed=None, view=None)


DYNAMIC_ITEMS = (DynamicLeaderLinkApproveButton, DynamicLeaderLinkDenyButton)
