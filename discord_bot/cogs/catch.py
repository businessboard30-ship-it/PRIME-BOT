"""Discord catch hub (P1-06).

The hub is presentation-only in this phase: every category is reachable,
navigation is handled in-place, and later phases can attach real services to
the same callbacks without changing the interaction contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import discord
from discord import app_commands
from discord.ext import commands

from modules.catch_theme import button_style, state_color


@dataclass(frozen=True)
class HubCategory:
    key: str
    label: str
    description: str
    actions: tuple[tuple[str, str], ...]


CATEGORIES: tuple[HubCategory, ...] = (
    HubCategory("play", "Play", "Find wild creatures and claim today's rewards.", (("Encounter", "Find a creature"), ("Daily", "Claim your daily reward"), ("Wild zone", "See active spawns"))),
    HubCategory("collect", "Collect", "Manage your creatures, dex and items.", (("Collection", "View your creatures"), ("Dex", "Track discovered species"), ("Inventory", "Use balls and items"))),
    HubCategory("social", "Social", "Trade, gift and compare your collection.", (("Trade", "Trade with another player"), ("Gifts", "Send a gift"), ("Leaderboard", "Compare catch totals"))),
    HubCategory("economy", "Economy", "Earn and spend coins through catching.", (("Shop", "Browse the item shop"), ("Sell", "Sell duplicates"), ("Wallet", "View your balance"))),
    HubCategory("battle", "Battle", "Prepare your team for future battles.", (("Team", "Choose your battle team"), ("Battle", "Challenge a player"), ("Moves", "Review creature moves"))),
    HubCategory("info", "Info", "Learn how the catch game works.", (("Guide", "Read the game guide"), ("Rules", "View server rules"), ("Status", "View game status"))),
)


def category_for(key: str) -> HubCategory:
    return next((category for category in CATEGORIES if category.key == key), CATEGORIES[0])


def component_count(view: discord.ui.View) -> int:
    """Count children recursively for the Discord 25-component guard."""
    return sum(1 + component_count(child) for child in view.children if isinstance(child, discord.ui.View)) + len(view.children)


class CatchHubView(discord.ui.View):
    def __init__(self, *, category: str = "play"):
        super().__init__(timeout=900)
        self.category = category_for(category)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        select = discord.ui.Select(
            placeholder="Choose a catch category",
            options=[
                discord.SelectOption(
                    label=item.label,
                    value=item.key,
                    description=item.description,
                    default=item.key == self.category.key,
                )
                for item in CATEGORIES
            ],
            custom_id="catch:hub:category",
        )
        select.callback = self._select_category
        self.add_item(select)

        pinned = discord.ui.Button(label="Encounter", style=button_style("main"), custom_id="catch:hub:encounter")
        pinned.callback = self._action("Encounter", "Find a creature")
        self.add_item(pinned)
        daily = discord.ui.Button(label="Daily", style=button_style("claim"), custom_id="catch:hub:daily")
        daily.callback = self._action("Daily", "Claim your daily reward")
        self.add_item(daily)

        for label, description in self.category.actions:
            button = discord.ui.Button(label=label, style=button_style("navigation"), custom_id=f"catch:hub:{self.category.key}:{label.lower()}")
            button.callback = self._action(label, description)
            self.add_item(button)

        home = discord.ui.Button(label="Home", style=button_style("navigation"), custom_id="catch:hub:home", row=4)
        home.callback = self._home
        self.add_item(home)

    async def _select_category(self, interaction: discord.Interaction) -> None:
        selected = interaction.data.get("values", ["play"])[0] if interaction.data else "play"
        self.category = category_for(selected)
        self._build()
        await interaction.response.edit_message(embed=build_hub_embed(self.category.key), view=self)

    def _action(self, label: str, description: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(f"**{label}** is ready for this server. {description}", ephemeral=True)
        return callback

    async def _home(self, interaction: discord.Interaction) -> None:
        self.category = CATEGORIES[0]
        self._build()
        await interaction.response.edit_message(embed=build_hub_embed("play"), view=self)


def build_hub_embed(category: str = "play") -> discord.Embed:
    selected = category_for(category)
    embed = discord.Embed(
        title="Catch hub",
        description="Choose a category below to explore the Discord catch game.",
        colour=state_color("info"),
    )
    embed.add_field(name=selected.label, value=selected.description, inline=False)
    embed.add_field(name="Available now", value="\n".join(f"**{label}** — {description}" for label, description in selected.actions), inline=False)
    embed.set_footer(text="Use Home to return to Play. This panel expires after 15 minutes.")
    return embed


class CatchHubDynamicButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"catch:hub:(?:(?P<category>[a-z-]+):)?(?P<action>[a-z-]+)",
):
    """Reconstruct catch hub buttons from their custom_id after a restart.

    Hub buttons encode their category in the custom ID when applicable. The
    optional group also accepts the global Home, Encounter, and Daily buttons.
    """

    def __init__(
        self,
        item: discord.ui.Button,
        *,
        action: str,
        category: str | None = None,
    ):
        super().__init__(item)
        self.action = action
        self.category = category

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "CatchHubDynamicButton":
        return cls(
            item,
            action=match.group("action"),
            category=match.group("category"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "home":
            await interaction.response.send_message(
                embed=build_hub_embed(), view=CatchHubView(), ephemeral=True
            )
            return
        labels = {
            "encounter": ("Encounter", "Find a creature"),
            "daily": ("Daily", "Claim your daily reward"),
        }
        if self.category:
            selected = category_for(self.category)
            labels.update({
                label.lower().replace(" ", "-"): (label, description)
                for label, description in selected.actions
            })
        label, description = labels.get(
            self.action,
            (self.action.replace("-", " ").title(), "Open this catch hub section"),
        )
        await interaction.response.send_message(
            f"**{label}** is ready for this server. {description}", ephemeral=True
        )


DYNAMIC_ITEMS = (CatchHubDynamicButton,)


class CatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="catch", description="Open the creature-catching hub")
    async def catch(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_hub_embed(), view=CatchHubView(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CatchCog(bot))


__all__ = ["CATEGORIES", "CatchHubView", "build_hub_embed", "category_for", "component_count"]


assert component_count(CatchHubView()) <= 25
assert all(len(category.actions) <= 3 for category in CATEGORIES)
