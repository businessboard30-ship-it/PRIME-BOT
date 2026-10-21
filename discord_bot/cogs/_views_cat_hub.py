# path: discord_bot/cogs/_views_cat_hub.py
"""The /cat menu: a small private wizard. /cat and the Menu button on every spawn open the same card.

The card shows when this server's next cat comes and links to a cat that is waiting to be adopted, then a menu lists
everything the game can do. Choosing an entry runs the SAME button the rest of the game already uses (My cats, Market,
Daily care, ...), so nothing is duplicated and every screen behaves exactly as before.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord

from modules import cat_adopt as ca
from modules import cat_help
from modules import cat_hub
from modules import cat_perms
from modules import cat_setup as cs
from discord_bot.cogs import _cat_visibility as vis

logger = logging.getLogger(__name__)
PINK = discord.Color.from_rgb(255, 182, 193)


def _button_for(key: str):
    """The existing game button behind a menu entry (imported here so this module never loops back on the others)."""
    from discord_bot.cogs import _views_cat_adopt as adopt
    from discord_bot.cogs import _views_cat_arena as arena
    from discord_bot.cogs import _views_cat_features as feat
    from discord_bot.cogs import _views_cat_home as home
    from discord_bot.cogs import _views_cat_life as life
    from discord_bot.cogs import _views_cat_market as market
    table = {
        "mine": adopt.MyCatsButton, "help": adopt.HelpButton,
        "market": market.MarketButton, "board": market.LeaderboardButton,
        "care": feat.CareButton, "quests": feat.QuestsButton, "badges": feat.BadgesButton,
        "wishlist": feat.WishlistButton, "profile": feat.ProfileButton, "cosmetics": feat.CosmeticsButton,
        "life": life.CatLifeButton, "arena": arena.ArenaButton, "home": home.HomeButton,
    }
    cls = table.get(key)
    return cls() if cls else None


async def status(interaction: discord.Interaction) -> list:
    """The 'next cat / a cat is waiting' lines for the server this was opened in. Never raises."""
    guild_id = getattr(interaction, "guild_id", None)
    if not guild_id:
        return []
    try:
        clone_id = getattr(interaction.client, "clone_id", None)
        cfg = await cs.get_config(guild_id, clone_id)
        next_at = cs.upcoming(cfg, await cs.get_next_at(guild_id, clone_id)) if cfg.enabled else None
        spawn = await ca.open_spawn(guild_id, clone_id)
        return cat_hub.status_lines(True, bool(cfg.enabled), int(next_at.timestamp()) if next_at else None,
                                    cat_hub.jump_url(guild_id, spawn))
    except Exception:
        logger.debug("[cat-hub] couldn't read the server's cat status", exc_info=True)
        return []


def hub_text(lines: list) -> str:
    """The heading and status lines at the top of the menu card."""
    return f"## \U0001F431 Cat menu\n{cat_hub.card_text(lines)}"


class HubSelect(discord.ui.Select):
    def __init__(self, hub: Optional["HubView"] = None):
        self.hub = hub
        super().__init__(
            placeholder="What would you like to do?",
            options=[discord.SelectOption(label=label, value=key, emoji=emoji, description=desc)
                     for key, emoji, label, desc in cat_hub.MENU])

    async def callback(self, interaction: discord.Interaction):
        btn = _button_for(self.values[0])
        if btn is None:
            await interaction.response.send_message("That isn't available right now.", ephemeral=True)
            return
        # The chosen screen replaces this card in place (vis.send sees the origin) and gets a Back to menu button.
        token = vis.menu_origin.set({"picture_url": getattr(self.hub, "picture_url", None)})
        try:
            await btn.callback(interaction)
        finally:
            vis.menu_origin.reset(token)


class BackToMenu(discord.ui.Button):
    """Back to the Cat menu card, in the same message."""

    def __init__(self):
        super().__init__(label="Back to menu", emoji="◀", style=discord.ButtonStyle.secondary, custom_id=vis.BACK_ID)

    async def callback(self, interaction: discord.Interaction):
        picture = vis.hub_picture(getattr(interaction.message, "id", None))
        lines = await status(interaction)
        view = HubView(picture, can_setup=await _can_setup(interaction), lines=lines)
        if getattr(interaction, "guild_id", None) is not None:
            vis.lock_to_owner(view, interaction.user.id)
        await interaction.response.edit_message(view=view)


class _NextCatButton(discord.ui.Button):
    def __init__(self, hub: "HubView"):
        super().__init__(label="Next cat", emoji="⏰", style=discord.ButtonStyle.secondary)
        self.hub = hub

    async def callback(self, interaction: discord.Interaction):
        self.hub.lines = await status(interaction)
        self.hub.build()
        await interaction.response.edit_message(view=self.hub)


class _ShareButton(discord.ui.Button):
    def __init__(self, hub: "HubView"):
        super().__init__(label="Share this cat", emoji="📣", style=discord.ButtonStyle.secondary)
        self.hub = hub

    async def callback(self, interaction: discord.Interaction):
        picture_url = self.hub.picture_url
        if not picture_url:
            await interaction.response.send_message("There's no cat picture on this card to share.", ephemeral=True)
            return
        import random
        from discord_bot.cogs._views_cat_adopt import spawn_post
        from discord_bot.cogs.cat import CAT_TITLES
        try:      # a public V2 card: title and picture, no buttons
            await interaction.channel.send(view=spawn_post(None, random.choice(CAT_TITLES), str(picture_url)),
                                           allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError):
            await interaction.response.send_message(
                "I couldn't post in this channel. I need **Send Messages** and **Embed Links** here.", ephemeral=True)
            return
        await interaction.response.send_message("📣 Shared in the channel.", ephemeral=True)


class HubView(discord.ui.LayoutView):
    """Components V2 menu card: status, the cat picture, the menu and three buttons. Locked to whoever opened it."""

    def __init__(self, picture_url: Optional[str] = None, can_setup: bool = False, lines: Optional[list] = None):
        super().__init__(timeout=600)
        self.picture_url = picture_url
        self.can_setup = can_setup
        self.lines = list(lines or [])
        self.build()

    def build(self) -> None:
        self.clear_items()
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.Section(hub_text(self.lines), accessory=discord.ui.Thumbnail(cat_help.RING_URL)))
        if self.picture_url:
            box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(str(self.picture_url), description="Cat")))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(HubSelect(self)))
        buttons = [_NextCatButton(self), _ShareButton(self)]
        if self.can_setup:
            buttons.append(_SetupButton())
        box.add_item(discord.ui.ActionRow(*buttons))
        box.add_item(discord.ui.TextDisplay(f"-# {cat_help.FOOTER}"))
        self.add_item(box)


class _SetupButton(discord.ui.Button):
    """Server managers only: opens the same Cat setup wizard as /catsetup, so spawns are one tap away."""

    def __init__(self):
        super().__init__(label="Set up spawns", emoji="🛠️", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_shared import user_can_manage_guild
        guild = interaction.guild
        if guild is None or not await user_can_manage_guild(guild, interaction.user.id):
            await interaction.response.send_message("You need the **Manage Server** permission to set up cats.",
                                                    ephemeral=True)
            return
        channel = interaction.channel
        ok = lambda c: isinstance(c, discord.TextChannel) and not cat_perms.missing_names(c.permissions_for(guild.me))
        if not ok(channel):
            channel = next((c for c in [guild.system_channel, *guild.text_channels] if ok(c)), None)
        if channel is None:
            await interaction.response.send_message(
                "I couldn't find a channel I can post in. Give me **Send Messages** and **Embed Links** in one and try again.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from discord_bot.cogs._views_cat_setup import open_setup
            await open_setup(interaction.client, guild, getattr(interaction.client, "clone_id", None),
                             interaction.user.id, channel)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("[cat-hub] couldn't post the Cat setup in guild %s", guild.id)
            await interaction.followup.send("I couldn't post the Cat setup there. Check my permissions and try /catsetup.",
                                            ephemeral=True)
            return
        where = "here" if channel == interaction.channel else channel.mention
        await interaction.followup.send(f"🐱 Cat setup opened {where}. Only you can use it.", ephemeral=True)


async def _can_setup(interaction: discord.Interaction) -> bool:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return False
    try:
        from discord_bot.cogs._views_shared import user_can_manage_guild
        return bool(await user_can_manage_guild(guild, interaction.user.id))
    except Exception:
        return False


async def open_hub(interaction: discord.Interaction, picture_url: Optional[str] = None, note: Optional[str] = None,
                   deferred: Optional[bool] = None) -> None:
    """Shows the menu card privately. `note` is one extra line on top (e.g. 'no picture right now'). `deferred` says the
    interaction was already deferred (ephemeral); when None it is looked up."""
    lines = await status(interaction)
    if note:
        lines.insert(0, note)
    view = HubView(picture_url, can_setup=await _can_setup(interaction), lines=lines)
    # Public in a server (locked to whoever opened it); the interaction was deferred with the same visibility.
    if deferred is None:
        deferred = interaction.response.is_done()
    await vis.send(interaction, view=view, deferred=deferred)
