# path: discord_bot/cogs/_views_quickstart_pointer.py

"""
Compact quick-start message (replaces the long paged join DM as the default).

One short line pointing at the Server Owners Panel plus a few buttons:

* Open server panel  - opens the panel (same screen as /serversetup). Works
  where there is a server (the /start reply and the in-server copy). In a DM
  there is no server to open it in, so the DM gets a "Go to server" link
  button instead and this button only explains where to run /serversetup.
* Full setup guide   - swaps this same message into the old long guide
  (build_join_dm_view), so nothing from the old DM is lost.

DynamicItem buttons, like the rest of the join DM: they survive restarts and
never time out. guild_id/clone_id live in the custom_id. Imports from
_views_join_dm and _views_server_panel are lazy to avoid circular imports.
"""

import re
import logging

import discord

logger = logging.getLogger(__name__)

TITLE = "🚀 Thanks for adding me!"


def _clone_part(clone_id) -> str:
    return "-" if clone_id is None else str(clone_id)


def _parse(match: "re.Match"):
    clone_part = match.group(2)
    return int(match.group(1)), (None if clone_part == "-" else int(clone_part))


def pointer_text(guild_name: str) -> str:
    return (f"## {TITLE}\n"
            f"Set up **{guild_name or 'your server'}** from one place: run `/serversetup` in your server.")


class OpenPanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^join_dm_panel:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Open server panel", style=discord.ButtonStyle.primary, emoji="🛠️",
            custom_id=f"join_dm_panel:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _parse(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Open your server and run `/serversetup` there to open the panel.", ephemeral=True)
            return
        if interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "This button belongs to a different server. Run `/serversetup` here instead.", ephemeral=True)
            return
        from discord_bot.cogs._views_server_panel import open_home
        await open_home(interaction)


class FullGuideButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^join_dm_full:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Full setup guide", style=discord.ButtonStyle.secondary, emoji="📖",
            custom_id=f"join_dm_full:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _parse(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        # Defer first: the rebuild below reads the database.
        await interaction.response.defer()
        from discord_bot.cogs._views_join_dm import (
            FEATURE_TOGGLES, _enabled_feature_keys, _build_main_join_dm_parts, _fresh_ad_for_join_dm,
            _offer_state_from_message, build_join_dm_view,
        )
        guild = interaction.client.get_guild(self.guild_id)
        enabled = await _enabled_feature_keys(self.guild_id, self.clone_id)
        intro, title, notices = await _build_main_join_dm_parts(interaction.client, self.guild_id, self.clone_id)
        fresh_ad = await _fresh_ad_for_join_dm(interaction.client)
        view = build_join_dm_view(
            self.guild_id, clone_id=self.clone_id, feature_keys=list(FEATURE_TOGGLES.keys()), page=0,
            intro=intro, title=title, notices=notices, enabled_keys=enabled,
            guild_name=guild.name if guild else None,
            join_offer=_offer_state_from_message(interaction.message), fresh_ad=fresh_ad,
        )
        await interaction.edit_original_response(view=view)


class QuickstartPointerView(discord.ui.LayoutView):
    """The short quick-start message. `dm=True` swaps the panel button for a
    "Go to server" link (a DM has no server to open the panel in)."""

    def __init__(self, guild_id: int, clone_id=None, guild_name: str = None, jump_url: str = None,
                 dm: bool = False, join_offer: dict = None):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(pointer_text(guild_name)))

        if join_offer and join_offer.get("show_invite"):
            from discord_bot.cogs._views_join_dm import _JoinOfferInviteButton
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                f"One more thing about **{guild_name or 'your server'}**: I keep a private admin registry "
                "of servers I'm in (only my bot owner sees it) and it's more useful with an invite link on "
                "file. I'd need to create a fresh one. Only allow this if you're OK with that; everything "
                "else works the same either way."
            ))
            container.add_item(discord.ui.ActionRow(
                _JoinOfferInviteButton("allow", guild_id, clone_id),
                _JoinOfferInviteButton("decline", guild_id, clone_id),
            ))

        buttons: list = []
        if dm:
            if jump_url:
                buttons.append(discord.ui.Button(label="Go to server", style=discord.ButtonStyle.link, url=jump_url))
        else:
            buttons.append(OpenPanelButton(guild_id, clone_id))
        buttons.append(FullGuideButton(guild_id, clone_id))
        container.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(container)


DYNAMIC_ITEMS = (OpenPanelButton, FullGuideButton)
