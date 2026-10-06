"""Trainer profile card for the catch game (Phase 3: P3-07).

Opened from the Collection screen as an edit of the same ephemeral message, so Back returns
to the list. Read-only: defers first (rule B.6), checks the ``view`` gate, then one call to
``load_profile`` (scoped by user and clone). Only the owner can press its buttons.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable

import discord

from modules import catch_emoji as emoji
from modules.catch_card import trainer_card_png
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_profile import ProfileCreature, TrainerProfile, load_profile
from modules.catch_theme import button_style, rarity_color

logger = logging.getLogger(__name__)

BackCallback = Callable[[discord.Interaction], Awaitable[None]]


def _creature_line(c: ProfileCreature) -> str:
    flag_text = emoji.flags(shiny=c.shiny, special=c.special)
    label = f"{c.nickname} ({c.name})" if c.nickname else c.name
    return (
        f"{emoji.mark('rarity', c.rarity)} **{discord.utils.escape_markdown(label)}** · Lv.{c.level}"
        f"{(' ' + flag_text) if flag_text else ''}"
    )


def profile_embed(profile: TrainerProfile, display_name: str) -> discord.Embed:
    top = profile.buddy or profile.rarest
    colour = rarity_color(top.rarity, shiny=top.shiny, special=top.special) if top else rarity_color("common")
    embed = discord.Embed(
        title=f"{emoji.mark('ui', 'profile')} {text('profile.title', name=discord.utils.escape_markdown(display_name))}"[:256],
        colour=colour,
    )
    embed.add_field(
        name=text("profile.buddy"),
        value=f"{emoji.mark('flag', 'buddy')} {_creature_line(profile.buddy)}" if profile.buddy else text("profile.no_buddy"),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji.mark('ui', 'rarest')} {text('profile.rarest')}",
        value=_creature_line(profile.rarest) if profile.rarest else text("profile.no_rarest"),
        inline=False,
    )
    embed.add_field(
        name=text("profile.catches"),
        value=text("profile.catches_value", total=profile.total_catches, streak=profile.catch_streak,
                   best=profile.best_streak, daily=profile.daily_streak,
                   streak_icon=emoji.mark("ui", "streak"), daily_icon=emoji.mark("ui", "daily")),
        inline=True,
    )
    embed.add_field(
        name=text("profile.collection"),
        value=text("profile.collection_value", owned=profile.owned, shinies=profile.shinies, specials=profile.specials,
                   icon=emoji.mark("ui", "collection"), shiny_icon=emoji.mark("flag", "shiny"),
                   special_icon=emoji.mark("flag", "special")),
        inline=True,
    )
    embed.add_field(
        name=text("profile.dex"),
        value=text("profile.dex_value", percent=profile.dex_percent, caught=profile.dex_caught,
                   total=profile.dex_total, seen=profile.dex_seen, icon=emoji.mark("ui", "dex")),
        inline=True,
    )
    return embed


CARD_FILE = "trainer.png"


async def profile_message(profile: TrainerProfile, display_name: str) -> dict:
    """Embed plus drawn trainer card for ``edit_original_response``.

    The embed keeps every field (it also carries the numbers the card leaves out, such as
    best streak and seen count). If drawing fails the screen is the text embed alone.
    """
    embed = profile_embed(profile, display_name)
    try:
        data = await trainer_card_png(profile)
    except Exception:
        logger.exception("Catch trainer card render failed")
        return {"embed": embed, "attachments": []}
    embed.set_image(url=f"attachment://{CARD_FILE}")
    return {"embed": embed, "attachments": [discord.File(io.BytesIO(data), filename=CARD_FILE)]}


class ProfileView(discord.ui.View):
    def __init__(self, user_id: int, *, back: BackCallback):
        super().__init__(timeout=600)
        self.user_id, self.back = user_id, back
        button = discord.ui.Button(
            label=text("profile.back"), emoji=emoji.mark("ui", "back"), style=button_style("navigation"),
        )
        button.callback = self._back
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.back(interaction)


async def open_profile(
    interaction: discord.Interaction, user_id: int, clone_id: int | None, *, back: BackCallback,
) -> None:
    """Replace the (already deferred) message with the trainer card."""
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        profile = await load_profile(user_id, clone_id)
    except Exception:
        logger.exception("Catch profile load failed user=%s", user_id)
        await interaction.followup.send(text("profile.error"), ephemeral=True)
        return
    name = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", None) or "Trainer"
    await interaction.edit_original_response(
        content=None, view=ProfileView(user_id, back=back), **await profile_message(profile, str(name)),
    )


__all__ = ["CARD_FILE", "ProfileView", "open_profile", "profile_embed", "profile_message"]
