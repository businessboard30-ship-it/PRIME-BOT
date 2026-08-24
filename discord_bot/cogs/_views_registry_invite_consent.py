# path: discord_bot/cogs/_views_registry_invite_consent.py

"""
Opt-in replacement for the invite auto-creation Top.gg flagged as a
privacy issue (see bot.py's _best_effort_invite docstring). That
function now only ever returns a vanity URL or an EXISTING invite —
both already visible/available on the server. If neither exists, this
module DMs the owner asking if the bot can create one purely so the
bot owner has a link for their own /admin guilds registry. A fresh
invite is only ever created after the owner clicks "Allow" here.

DynamicItem (not a plain View) so the buttons keep working across a
bot restart — same convention as bump.py's DynamicBumpApproveButton and
_views_leaderboard_links.py's approve/deny buttons.
"""

import logging

import discord

from database import db

logger = logging.getLogger(__name__)


async def offer_registry_invite_consent(bot, guild: discord.Guild) -> None:
    """Called from bot.py's _handle_new_guild only when _best_effort_invite
    found nothing (no vanity URL, no existing invite readable). Best-effort
    throughout — a DM that fails to send (owner has DMs closed, left,
    etc) just means the registry entry stays without an invite_url,
    same as it always could before this feature existed."""
    try:
        owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
    except (discord.HTTPException, discord.Forbidden):
        owner = None
    if owner is None or owner.bot:
        return

    view = discord.ui.View(timeout=None)
    view.add_item(DynamicRegistryInviteAllowButton(guild.id))
    view.add_item(DynamicRegistryInviteDeclineButton(guild.id))

    try:
        await owner.send(
            f"One more thing about **{guild.name}** — I keep a private admin registry of servers I'm in "
            "(only visible to my own bot owner, never shared publicly), and it's more useful with an invite "
            "link on file. Your server doesn't have a vanity URL or a readable existing invite, so I'd need "
            "to create a fresh one. Only do this if you're OK with that — otherwise just decline, everything "
            "else about the bot works exactly the same either way.",
            view=view,
        )
    except (discord.HTTPException, discord.Forbidden):
        pass


async def _create_invite_for_registry(bot, guild_id: int) -> str | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if perms.create_instant_invite:
            try:
                invite = await channel.create_invite(
                    max_age=0, reason="Registry entry for /admin guilds — owner-approved via DM consent"
                )
                return invite.url
            except (discord.HTTPException, discord.Forbidden):
                continue
    return None


class DynamicRegistryInviteAllowButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"registryinvite:allow:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Allow", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"registryinvite:allow:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            invite_url = await _create_invite_for_registry(interaction.client, self.guild_id)
        except Exception:
            logger.exception("Failed creating registry invite for guild %s after owner consent", self.guild_id)
            await interaction.edit_original_response(content="Something went wrong creating the invite — check the bot logs.", view=None)
            return

        if not invite_url:
            await interaction.edit_original_response(
                content="Thanks for saying yes — but I don't have permission to create an invite in any channel there, so I couldn't make one.",
                view=None,
            )
            return

        clone_id = getattr(interaction.client, "clone_id", None)
        await db.set_guild_invite_url(self.guild_id, invite_url, clone_id=clone_id)
        await interaction.edit_original_response(content="✅ Thanks — invite link saved to the registry.", view=None)


class DynamicRegistryInviteDeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"registryinvite:decline:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="No thanks", style=discord.ButtonStyle.secondary,
                               custom_id=f"registryinvite:decline:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(content="No problem — nothing was created, everything else works the same.", view=None)


DYNAMIC_ITEMS = (DynamicRegistryInviteAllowButton, DynamicRegistryInviteDeclineButton)
