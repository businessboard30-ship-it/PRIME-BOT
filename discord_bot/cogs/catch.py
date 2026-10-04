"""Discord catch hub (P1-06).

The hub is presentation-only in this phase: every category is reachable,
navigation is handled in-place, and later phases can attach real services to
the same callbacks without changing the interaction contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules.catch_scheduler import run_scheduler_batch
from modules.catch_setup import CatchSetup, SPEED_PRESETS, create_wild_zone_name, load_setup, save_setup, test_spawn_payload
from modules.catch_gate import check_player_allowed, set_feature_flag
from modules.catch_i18n import text
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

        setup = discord.ui.Button(label="Server setup", style=button_style("navigation"), custom_id="catch:hub:setup", row=4)
        setup.callback = self._setup
        self.add_item(setup)
        home = discord.ui.Button(label="Home", style=button_style("navigation"), custom_id="catch:hub:home", row=4)
        home.callback = self._home
        self.add_item(home)

    async def _setup(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        setup = await load_setup(guild_id) if guild_id is not None else CatchSetup()
        await interaction.response.send_message(
            embed=build_setup_embed(setup),
            view=CatchSetupView(setup, guild_id=guild_id),
            ephemeral=True,
        )

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
        title=text("hub.title"),
        description=text("hub.description"),
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


def build_setup_embed(setup: CatchSetup) -> discord.Embed:
    errors = setup.validate()
    embed = discord.Embed(
        title=text("setup.title"),
        description=text("setup.description"),
        colour=state_color("success" if setup.enabled else "info"),
    )
    embed.add_field(name="Status", value=setup.status_line(), inline=False)
    embed.add_field(
        name="Spawn channels",
        value=", ".join(f"<#{channel_id}>" for channel_id in setup.spawn_channel_ids) or "Not selected",
        inline=False,
    )
    embed.add_field(
        name="Speed",
        value=f"{setup.speed_preset.title()} ({setup.spawn_every_n_messages} messages / {setup.min_seconds_between_spawns}s)",
        inline=False,
    )
    if setup.encounter_channel_ids:
        embed.add_field(name="Encounter panel", value=", ".join(f"<#{channel_id}>" for channel_id in setup.encounter_channel_ids), inline=False)
    if setup.rare_ping_role_id:
        embed.add_field(name="Rare-spawn role", value=f"<@&{setup.rare_ping_role_id}>", inline=False)
    if errors:
        embed.add_field(name="Needs attention", value="\n".join(errors), inline=False)
    embed.set_footer(text="Changes are staged in this panel until the setup is saved.")
    return embed


class CatchSetupView(discord.ui.View):
    """Owner setup controls shared by the server panel and catch hub."""

    def __init__(self, setup: CatchSetup | None = None, *, guild_id: int | None = None):
        super().__init__(timeout=600)
        self.setup = setup or CatchSetup()
        self.guild_id = guild_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        toggle = discord.ui.Button(
            label="Turn off catching" if self.setup.enabled else "Turn on catching",
            style=button_style("danger" if self.setup.enabled else "claim"),
            custom_id="catch:setup:toggle",
            row=0,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)

        speed = discord.ui.Select(
            placeholder=f"Spawn speed: {self.setup.speed_preset.title()}",
            options=[
                discord.SelectOption(label=name.title(), value=name, default=name == self.setup.speed_preset)
                for name in SPEED_PRESETS
            ],
            custom_id="catch:setup:speed",
            row=1,
        )
        speed.callback = self._speed
        self.add_item(speed)

        channels = discord.ui.ChannelSelect(
            placeholder="Pick spawn channels",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=5,
            custom_id="catch:setup:channels",
            row=2,
        )
        channels.callback = self._channels
        self.add_item(channels)
        wild_zone = discord.ui.Button(label="Create wild-zone", style=button_style("navigation"), custom_id="catch:setup:wild-zone", row=3)
        wild_zone.callback = self._wild_zone
        self.add_item(wild_zone)
        test = discord.ui.Button(label="Test spawn", style=button_style("main"), custom_id="catch:setup:test", row=3)
        test.callback = self._test_spawn
        self.add_item(test)
        encounter = discord.ui.Button(
            label="Disable encounters" if self.setup.encounter_channel_ids else "Enable encounters",
            style=button_style("danger" if self.setup.encounter_channel_ids else "claim"),
            custom_id="catch:setup:encounters",
            row=3,
        )
        encounter.callback = self._toggle_encounters
        self.add_item(encounter)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if self.guild_id is not None:
            if not interaction.permissions.manage_guild:
                await interaction.response.send_message("Only members with Manage Server can change Catch setup.", ephemeral=True)
                return
            await save_setup(self.guild_id, self.setup)
            await set_feature_flag(
                self.guild_id,
                None,
                "game",
                self.setup.enabled,
                updated_by=interaction.user.id,
                reason="Catch owner setup toggle",
            )
        self._build()
        await interaction.response.edit_message(embed=build_setup_embed(self.setup), view=self)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        candidate = replace(self.setup, enabled=not self.setup.enabled)
        errors = candidate.validate()
        if candidate.enabled and errors:
            await interaction.response.send_message("Select at least one spawn channel before enabling catching.", ephemeral=True)
            return
        self.setup = candidate
        await self._refresh(interaction)

    async def _speed(self, interaction: discord.Interaction) -> None:
        selected = interaction.data.get("values", [self.setup.speed_preset])[0] if interaction.data else self.setup.speed_preset
        self.setup = self.setup.with_speed(selected)
        await self._refresh(interaction)

    async def _channels(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        channel_ids = tuple(dict.fromkeys(int(value) for value in values))
        if not channel_ids:
            await interaction.response.send_message(text("setup.text_channel"), ephemeral=True)
            return
        self.setup = self.setup.with_spawn_channels(channel_ids)
        await self._refresh(interaction)

    async def _toggle_encounters(self, interaction: discord.Interaction) -> None:
        encounter_channels = self.setup.spawn_channel_ids if not self.setup.encounter_channel_ids else ()
        self.setup = replace(self.setup, encounter_channel_ids=encounter_channels)
        await self._refresh(interaction)

    async def _wild_zone(self, interaction: discord.Interaction) -> None:
        existing = {channel.name for channel in getattr(interaction.guild, "channels", ())}
        await interaction.response.send_message(f"Create **#{create_wild_zone_name(existing)}** in this server, then select it as a spawn channel.", ephemeral=True)

    async def _test_spawn(self, interaction: discord.Interaction) -> None:
        channel_id = self.setup.spawn_channel_ids[0] if self.setup.spawn_channel_ids else None
        payload = test_spawn_payload(channel_id=channel_id)
        await interaction.response.send_message(text("setup.test_queued", payload=payload), ephemeral=True)


class CatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._scheduler.start()

    def cog_unload(self) -> None:
        self._scheduler.cancel()

    @tasks.loop(seconds=30)
    async def _scheduler(self) -> None:
        batch = await run_scheduler_batch()
        for row in batch.expired_ids:
            # Message deletion is intentionally best-effort; the DB claim is
            # the durable state transition that makes restarts safe.
            del row

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="catch", description="Open the creature-catching hub")
    async def catch(self, interaction: discord.Interaction) -> None:
        gate = await check_player_allowed(
            interaction.user.id,
            interaction.guild_id,
            "view",
        )
        if not gate.allowed:
            await interaction.response.send_message(
                f"Catch is currently unavailable: {gate.reason or 'disabled'}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=build_hub_embed(), view=CatchHubView(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CatchCog(bot))


__all__ = [
    "CATEGORIES",
    "CatchHubView",
    "CatchSetupView",
    "build_hub_embed",
    "build_setup_embed",
    "category_for",
    "component_count",
]


assert component_count(CatchHubView()) <= 25
assert all(len(category.actions) <= 3 for category in CATEGORIES)
