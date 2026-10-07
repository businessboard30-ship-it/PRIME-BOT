"""Evolve button on the buddy level-up message (XP slice two).

The level-up card says READY TO EVOLVE; this adds the button that goes with it. The button only
exists when a level-based evolution is available at the new level. It opens the SAME confirm
screen as the creature detail screen (``EvolveConfirmView``), so every guard there still applies:
owner check, ``view`` gate, SQL ownership, one spend. Nothing here changes a creature.

Every callback defers first (rule B.6), then checks the gate, then reads the database.
"""

from __future__ import annotations

import logging

import discord

from discord_bot.cogs._views_catch_creature import (
    CreatureView,
    EvolveConfirmView,
    evolve_confirm_message,
)
from modules import catch_emoji as emoji
from modules.catch_creature import load_creature
from modules.catch_evolve import preview
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_notice import notice_for
from modules.catch_levelup_card import LEVELUP_FILE, evolve_ready, levelup_card_png, levelup_file
from modules.catch_profile import load_profile
from modules.catch_species import all_species
from modules.catch_theme import button_style, state_color

logger = logging.getLogger(__name__)


class LevelUpEvolveView(discord.ui.View):
    """One Evolve button under the level-up card. Only its owner can use it."""

    def __init__(self, user_id: int, clone_id: int | None, owned_id: int):
        super().__init__(timeout=600)
        self.user_id, self.clone_id, self.owned_id = user_id, clone_id, owned_id
        self._busy = False
        button = discord.ui.Button(
            label=text("creature.btn_evolve"), emoji=emoji.mark("ui", "evolve"),
            style=button_style("main"),
        )
        button.callback = self._evolve
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**notice_for("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _closed(self, interaction: discord.Interaction) -> None:
        """Back from the creature screen: the level-up message is stale by then, so close it."""
        await interaction.edit_original_response(
            content=text("levelup.evolve_done"), embed=None, view=None, attachments=[],
        )

    async def _evolve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._busy:
            await interaction.followup.send(**notice_for("creature.busy"), ephemeral=True)
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(
                    **notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True,
                )
                return
            detail = await load_creature(self.owned_id, self.user_id, self.clone_id)
            pv = await preview(self.owned_id, self.user_id, self.clone_id)
            if detail is None or pv is None:
                await interaction.followup.send(**notice_for("creature.not_found"), ephemeral=True)
                return
            if pv.eligibility.status != "ready":
                await interaction.followup.send(**notice_for(f"evolve.refused_{pv.eligibility.status}"), ephemeral=True)
                return
            creature_view = CreatureView(self.user_id, self.clone_id, detail, pv, back=self._closed)
            confirm = EvolveConfirmView(self.user_id, self.clone_id, creature_view)
            await interaction.edit_original_response(
                content=None, view=confirm, **await evolve_confirm_message(pv, detail),
            )
        except Exception:
            logger.exception("Catch level-up evolve failed user=%s creature=%s", self.user_id, self.owned_id)
            await interaction.followup.send(**notice_for("levelup.error"), ephemeral=True)
        finally:
            self._busy = False


def evolve_view_for(species: dict | None, creature, *, level: int, user_id: int,
                    clone_id: int | None) -> LevelUpEvolveView | None:
    """The button view, or None when there is nothing to evolve. Never raises."""
    try:
        if species is None or creature is None or not evolve_ready(species, level):
            return None
        return LevelUpEvolveView(user_id, clone_id, int(creature.id))
    except Exception:
        logger.warning("Level-up evolve button not built", exc_info=True)
        return None


async def send_levelup(interaction: discord.Interaction, xp, *, message_key: str = "xp.daily_level_up") -> None:
    """Best effort level-up card (with the Evolve button when it applies) as a new ephemeral message.

    Same rules as the catch path in ``catch.py``: only when the buddy really reached the new level,
    and any failure is logged and swallowed so it can never affect the finished action.
    """
    if xp is None or not getattr(xp, "leveled", False):
        return
    try:
        clone_id = getattr(interaction.client, "clone_id", None)
        buddy = (await load_profile(interaction.user.id, clone_id)).buddy
        species = all_species().get(buddy.species_id) if buddy is not None else None
        if buddy is None or species is None or buddy.level != xp.level_after:
            return
        data = await levelup_card_png(
            species, level_before=xp.level_before, level_after=xp.level_after, xp=xp.xp,
            shiny=buddy.shiny, special=buddy.special,
        )
        embed = discord.Embed(
            description=text(message_key, gained=xp.gained, level=xp.level_after),
            colour=state_color("success"),
        )
        embed.set_image(url=f"attachment://{LEVELUP_FILE}")
        extra = {}
        view = evolve_view_for(species, buddy, level=xp.level_after, user_id=interaction.user.id,
                               clone_id=clone_id)
        if view is not None:
            extra["view"] = view
        await interaction.followup.send(embed=embed, file=levelup_file(data), ephemeral=True, **extra)
    except Exception:
        logger.warning("Catch daily level-up card not sent", exc_info=True)


__all__ = ["LevelUpEvolveView", "evolve_view_for", "send_levelup"]
