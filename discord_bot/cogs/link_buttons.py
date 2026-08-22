"""
Discord port of handlers/link_buttons.py.

Reuses the SAME custom_link_buttons table Telegram writes to (chat_id is a
bare int with no platform column — same collision caveat already documented
in discover.py/admin.py: a Discord guild_id and a Telegram chat_id could in
theory collide. Accepted here for consistency with the rest of this port
rather than introducing a one-off schema change).

Discord's slash commands take structured params directly, so this collapses
Telegram's two-step "/addlinkbutton <label>" -> "now send me a URL" waiting
-state flow into one command: /linkbutton add label:<label> url:<url>.

Buttons show as an actual row of Discord link buttons via /linkbutton panel
(there's no single shared "Group Tools" menu on the Discord side the way
Telegram's get_link_button_rows() feeds into — this posts its own panel
message instead, and any other cog is free to call get_link_button_rows()
the same way to embed the row into a different message/view).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db

logger = logging.getLogger(__name__)

MAX_BUTTONS_PER_PANEL = 5  # Discord hard limit: 5 buttons per action row


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


async def _deny(interaction: discord.Interaction):
    msg = "You need the **Manage Server** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def get_link_button_rows(guild_id: int) -> list[discord.ui.Button]:
    """Flat list of discord.ui.Button(style=link) for this guild's configured
    custom link buttons — for embedding into another cog's View, same role
    handlers/link_buttons.py's get_link_button_rows() plays for Telegram's
    Group Tools menu."""
    buttons = await db.list_link_buttons(guild_id)
    return [discord.ui.Button(label=b["label"], style=discord.ButtonStyle.link, url=b["url"]) for b in buttons[:MAX_BUTTONS_PER_PANEL]]


class LinkButtonsPanelView(discord.ui.View):
    def __init__(self, buttons: list[discord.ui.Button]):
        super().__init__(timeout=None)
        for b in buttons:
            self.add_item(b)


class LinkButtonsCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    linkbutton = app_commands.guild_only()(app_commands.Group(name="linkbutton", description="Custom labeled link buttons for this server"))

    @linkbutton.command(name="add", description="Add a custom link button (e.g. 'Join Our Channel' -> a URL)")
    @app_commands.describe(label="Button text, e.g. 'Join Our Channel'", url="The link it opens (must start with http:// or https://)")
    async def add(self, interaction: discord.Interaction, label: str, url: str):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction)
            return
        if not url.startswith(("http://", "https://")):
            await interaction.followup.send(
                "That doesn't look like a link. Please provide a URL starting with http:// or https://.", ephemeral=True
            )
            return
        if len(label) > 80:
            await interaction.followup.send("Label is too long (Discord button labels max out at 80 characters).", ephemeral=True)
            return

        existing = await db.list_link_buttons(interaction.guild_id)
        if len(existing) >= MAX_BUTTONS_PER_PANEL and label not in {b["label"] for b in existing}:
            await interaction.followup.send(
                f"This server already has {MAX_BUTTONS_PER_PANEL} link buttons — Discord only allows "
                f"{MAX_BUTTONS_PER_PANEL} buttons per panel. Remove one first with `/linkbutton remove`.",
                ephemeral=True,
            )
            return

        ok = await db.add_link_button(interaction.guild_id, label, url, interaction.user.id)
        if ok:
            await interaction.followup.send(f"✅ \"{label}\" button saved. Use `/linkbutton panel` to post it.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Failed to save. Try again.", ephemeral=True)

    @linkbutton.command(name="list", description="List this server's configured link buttons")
    async def list_buttons(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction)
            return
        buttons = await db.list_link_buttons(interaction.guild_id)
        if not buttons:
            await interaction.followup.send("No custom link buttons yet. Add one with `/linkbutton add`.", ephemeral=True)
            return
        embed = discord.Embed(title="Custom link buttons", color=discord.Color.blurple())
        for b in buttons:
            embed.add_field(name=b["label"], value=b["url"], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @linkbutton.command(name="remove", description="Remove a link button by its label")
    @app_commands.describe(label="The exact label of the button to remove")
    async def remove(self, interaction: discord.Interaction, label: str):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction)
            return
        ok = await db.remove_link_button(interaction.guild_id, label)
        if ok:
            await interaction.followup.send(f"✅ Removed \"{label}\".", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ No button named \"{label}\" found.", ephemeral=True)

    @linkbutton.command(name="panel", description="Post this server's link buttons as a message in this channel")
    async def panel(self, interaction: discord.Interaction):
        buttons = await get_link_button_rows(interaction.guild_id)
        if not buttons:
            await interaction.response.send_message("No custom link buttons configured yet — add one with `/linkbutton add`.", ephemeral=True)
            return
        embed = discord.Embed(title="Links", color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, view=LinkButtonsPanelView(buttons))


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkButtonsCog(bot))
