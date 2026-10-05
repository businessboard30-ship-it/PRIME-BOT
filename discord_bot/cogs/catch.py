"""Discord catch hub (P1-06).

The hub is presentation-only in this phase: every category is reachable,
navigation is handled in-place, and later phases can attach real services to
the same callbacks without changing the interaction contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
import random
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from discord_bot.cogs._views_catch_collection import open_collection, open_dex
from discord_bot.cogs._views_catch_items import open_daily, open_inventory
from discord_bot.cogs._views_catch_guide import open_guide
from discord_bot.cogs._views_catch_rules import open_rules
from discord_bot.cogs._views_catch_sell import open_sell
from discord_bot.cogs._views_catch_status import open_status
from discord_bot.cogs._views_catch_wild import open_wild_zone
from discord_bot.cogs._views_catch_shop import open_shop, open_wallet
from discord_bot.cogs._views_shared import user_can_manage_guild
from modules.catch_scheduler import run_scheduler_batch
from modules.catch_reminders import Reminder, dispatch_due_reminders
from modules.catch_setup import CatchSetup, SPEED_PRESETS, load_setup, save_setup
from modules.catch_species import all_species
from modules.catch_spawn import attach_spawn_message, create_spawn, roll_spawn, spawn_embed_data
from modules.catch_trigger import ChannelTriggerState, consider_message
from modules.catch_encounter import EncounterOnCooldown, create_player_encounter
from modules.catch_gate import check_player_allowed, guild_allowed, set_feature_flag
from modules.catch_i18n import text
from modules.catch_items import ensure_starter_kit
from modules.catch_service import CatchBlocked, record_catch
from modules.catch_xp import grant_buddy_catch_xp
from modules.catch_theme import button_style, state_color
from modules.catch_throw import choice_label

logger = logging.getLogger(__name__)


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


REAL_ACTIONS = {
    "collection": open_collection, "dex": open_dex, "daily": open_daily, "inventory": open_inventory,
    "shop": open_shop, "wallet": open_wallet, "sell": open_sell,
    "wild-zone": open_wild_zone, "status": open_status,
    "guide": open_guide, "rules": open_rules,
}


class CatchHubView(discord.ui.View):
    def __init__(self, *, category: str = "play"):
        super().__init__(timeout=900)
        self.category = category_for(category)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        select = discord.ui.Select(
            placeholder=text("ui.choose_category"),
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

        pinned = discord.ui.Button(label=text("ui.encounter"), style=button_style("main"), custom_id="catch:hub:encounter")
        pinned.callback = self._encounter
        self.add_item(pinned)
        daily = discord.ui.Button(label=text("ui.daily"), style=button_style("claim"), custom_id="catch:hub:daily")
        daily.callback = self._action(text("ui.daily"), text("ui.daily_description"))
        self.add_item(daily)

        for label, description in self.category.actions:
            button = discord.ui.Button(label=label, style=button_style("navigation"), custom_id=f"catch:hub:{self.category.key}:{label.lower().replace(' ', '-')}")
            button.callback = self._action(label, description)
            self.add_item(button)

        setup = discord.ui.Button(label=text("ui.server_setup"), style=button_style("navigation"), custom_id="catch:hub:setup", row=4)
        setup.callback = self._setup
        self.add_item(setup)
        home = discord.ui.Button(label=text("ui.home"), style=button_style("navigation"), custom_id="catch:hub:home", row=4)
        home.callback = self._home
        self.add_item(home)

    async def _setup(self, interaction: discord.Interaction) -> None:
        # Acknowledge first (3-second rule); the permission lookup may hit the API.
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(text("encounter.server_only"), ephemeral=True)
            return
        if not await user_can_manage_guild(guild, interaction.user.id):
            await interaction.followup.send(text("setup.manage_server"), ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        setup = await load_setup(guild.id, clone_id)
        await interaction.followup.send(
            embed=build_setup_embed(setup),
            view=CatchSetupView(setup, guild_id=guild.id, clone_id=clone_id),
            ephemeral=True,
        )

    async def _encounter(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(text("encounter.server_only"), ephemeral=True)
            return
        # Acknowledge first (3-second rule): the gate check and the encounter insert hit the DB.
        await interaction.response.defer(ephemeral=True)
        clone_id = getattr(interaction.client, "clone_id", None)
        gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "encounter", clone_id)
        if not gate.allowed:
            await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
            return
        try:
            roll = roll_spawn(list(all_species().values()), random.Random())
            spawn_id = await create_player_encounter(
                user_id=interaction.user.id,
                clone_id=clone_id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                roll=roll,
            )
        except EncounterOnCooldown as exc:
            await interaction.followup.send(
                text("encounter.cooldown", ready_at=int(exc.ready_at.timestamp())),
                ephemeral=True,
            )
            return
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        data = spawn_embed_data(roll, expires_at=expires_at)
        embed = discord.Embed(title=data["title"], description=data["description"], colour=state_color("info"))
        embed.add_field(name=text("encounter.rarity"), value=data["rarity"])
        embed.add_field(name=text("encounter.level"), value=data["level"])
        embed.set_footer(text=text("encounter.footer", expires_at=data["expires_at"]))
        message = await interaction.followup.send(
            embed=embed, view=SpawnClaimView(spawn_id), ephemeral=True, wait=True
        )
        await attach_spawn_message(spawn_id, message.id)

    async def _select_category(self, interaction: discord.Interaction) -> None:
        selected = interaction.data.get("values", ["play"])[0] if interaction.data else "play"
        self.category = category_for(selected)
        self._build()
        await interaction.response.edit_message(embed=build_hub_embed(self.category.key), view=self)

    def _action(self, label: str, description: str):
        real = REAL_ACTIONS.get(label.lower().replace(" ", "-"))
        if real is not None:
            return real

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
    template=r"catch:hub:(?!category$)(?:(?P<category>[a-z-]+):)?(?P<action>[a-z-]+)",
):
    """Reconstruct catch hub buttons from their custom_id after a restart.

    Hub buttons encode their category in the custom ID when applicable. The
    optional group also accepts the global Home, Encounter, and Daily buttons.
    The ``category`` custom ID belongs to the select (CatchHubDynamicSelect); discord.py
    dispatches every matching template regardless of component type, so it is excluded here.
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
        if self.action == "encounter":
            await CatchHubView()._encounter(interaction)
            return
        if self.action == "setup":
            await CatchHubView()._setup(interaction)
            return
        if self.action in REAL_ACTIONS:
            await REAL_ACTIONS[self.action](interaction)
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


class CatchHubDynamicSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"catch:hub:category",
):
    """Restart-safe category select: rebuilds the hub for the chosen category."""

    def __init__(self, item: discord.ui.Select | None = None):
        super().__init__(item or discord.ui.Select(
            custom_id="catch:hub:category",
            options=[discord.SelectOption(label=c.label, value=c.key) for c in CATEGORIES],
        ))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
    ) -> "CatchHubDynamicSelect":
        return cls(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = (interaction.data or {}).get("values", ["play"])[0]
        view = CatchHubView(category=selected)
        await interaction.response.edit_message(embed=build_hub_embed(view.category.key), view=view)


DYNAMIC_ITEMS = (CatchHubDynamicButton, CatchHubDynamicSelect)


def build_setup_embed(setup: CatchSetup) -> discord.Embed:
    errors = setup.validate()
    embed = discord.Embed(
        title=text("setup.title"),
        description=text("setup.description"),
        colour=state_color("success" if setup.enabled else "info"),
    )
    embed.add_field(name="Status", value=setup.status_line(), inline=False)
    embed.add_field(name="Join DMs", value="Enabled" if setup.join_dm_enabled else "Disabled", inline=True)
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


async def publish_spawn(channel, *, guild_id: int, clone_id: int | None, setup: CatchSetup, source: str = "chat") -> int:
    """Roll, persist and publish one wild spawn; returns the spawn id.

    Persists first so the claim button always points at a real row, then links the
    posted message back. Raises on failure so callers can reset trigger state.
    """
    roll = roll_spawn(list(all_species().values()), random.Random())
    spawn_id = await create_spawn(
        guild_id=guild_id, clone_id=clone_id, channel_id=channel.id,
        roll=roll, expires_in=setup.despawn_seconds, source=source,
    )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=setup.despawn_seconds)
    data = spawn_embed_data(roll, expires_at=expires_at)
    embed = discord.Embed(title=data["title"], description=data["description"], colour=state_color("info"))
    embed.add_field(name=text("encounter.rarity"), value=data["rarity"])
    embed.add_field(name=text("encounter.level"), value=data["level"])
    embed.set_footer(text=text("encounter.footer", expires_at=data["expires_at"]))
    posted = await channel.send(embed=embed, view=SpawnClaimView(spawn_id))
    await attach_spawn_message(spawn_id, posted.id)
    return spawn_id


class CatchSetupView(discord.ui.View):
    """Owner setup controls shared by the server panel and catch hub."""

    def __init__(self, setup: CatchSetup | None = None, *, guild_id: int | None = None, clone_id: int | None = None):
        super().__init__(timeout=600)
        self.setup = setup or CatchSetup()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        # While the game is limited to the support server, turning it ON elsewhere is dimmed
        # (turning it off is always allowed). The gate refuses play in those servers regardless.
        locked = self.guild_id is not None and not self.setup.enabled and not guild_allowed(self.guild_id)
        toggle = discord.ui.Button(
            label=text("setup.support_only") if locked else text("setup.turn_off") if self.setup.enabled else text("setup.turn_on"),
            style=button_style("navigation") if locked else button_style("danger" if self.setup.enabled else "claim"),
            custom_id="catch:setup:toggle",
            disabled=locked,
            row=0,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)

        speed = discord.ui.Select(
            placeholder=text("setup.spawn_speed", speed=self.setup.speed_preset.title()),
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
            placeholder=text("setup.pick_channels"),
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=5,
            custom_id="catch:setup:channels",
            row=2,
        )
        channels.callback = self._channels
        self.add_item(channels)
        wild_zone = discord.ui.Button(label=text("setup.create_wild_zone"), style=button_style("navigation"), custom_id="catch:setup:wild-zone", row=3)
        wild_zone.callback = self._wild_zone
        self.add_item(wild_zone)
        test = discord.ui.Button(label=text("setup.test_spawn"), style=button_style("main"), custom_id="catch:setup:test", row=3)
        test.callback = self._test_spawn
        self.add_item(test)
        encounter = discord.ui.Button(
            label=text("setup.disable_encounters") if self.setup.encounter_channel_ids else text("setup.enable_encounters"),
            style=button_style("danger" if self.setup.encounter_channel_ids else "claim"),
            custom_id="catch:setup:encounters",
            row=3,
        )
        encounter.callback = self._toggle_encounters
        self.add_item(encounter)
        join_dm = discord.ui.Button(
            label=text("setup.disable_join_dms") if self.setup.join_dm_enabled else text("setup.enable_join_dms"),
            style=button_style("danger" if self.setup.join_dm_enabled else "claim"),
            custom_id="catch:setup:join-dm",
            row=4,
        )
        join_dm.callback = self._toggle_join_dm
        self.add_item(join_dm)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if self.guild_id is not None:
            # Acknowledge first (3-second rule); permission lookup and saves hit the API/DB.
            await interaction.response.defer()
            if not await user_can_manage_guild(interaction.guild, interaction.user.id):
                await interaction.followup.send(text("setup.manage_server"), ephemeral=True)
                return
            await save_setup(self.guild_id, self.setup, self.clone_id)
            await set_feature_flag(
                self.guild_id,
                self.clone_id,
                "game",
                self.setup.enabled,
                updated_by=interaction.user.id,
                reason="Catch owner setup toggle",
            )
            self._build()
            await interaction.edit_original_response(embed=build_setup_embed(self.setup), view=self)
            return
        self._build()
        await interaction.response.edit_message(embed=build_setup_embed(self.setup), view=self)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        if self.guild_id is not None and not self.setup.enabled and not guild_allowed(self.guild_id):
            await interaction.response.send_message(text("setup.support_only_detail"), ephemeral=True)
            return
        candidate = replace(self.setup, enabled=not self.setup.enabled)
        errors = candidate.validate()
        if candidate.enabled and errors:
            await interaction.response.send_message(text("setup.enable_channel"), ephemeral=True)
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

    async def _toggle_join_dm(self, interaction: discord.Interaction) -> None:
        self.setup = replace(self.setup, join_dm_enabled=not self.setup.join_dm_enabled)
        await self._refresh(interaction)

    async def _wild_zone(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None or self.guild_id is None:
            await interaction.followup.send(text("encounter.server_only"), ephemeral=True)
            return
        if not await user_can_manage_guild(guild, interaction.user.id):
            await interaction.followup.send(text("setup.manage_server"), ephemeral=True)
            return
        channel = discord.utils.get(guild.text_channels, name="wild-zone")
        if channel is None:
            if not guild.me.guild_permissions.manage_channels:
                await interaction.followup.send(text("setup.need_manage_channels"), ephemeral=True)
                return
            try:
                channel = await guild.create_text_channel("wild-zone", reason="Creature catching setup")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Catch setup could not create #wild-zone guild=%s", guild.id)
                await interaction.followup.send(text("setup.wild_zone_failed"), ephemeral=True)
                return
        self.setup = self.setup.with_spawn_channels([*self.setup.spawn_channel_ids, channel.id])
        await save_setup(self.guild_id, self.setup, self.clone_id)
        self._build()
        await interaction.edit_original_response(embed=build_setup_embed(self.setup), view=self)
        await interaction.followup.send(text("setup.wild_zone_ready", channel=channel.mention), ephemeral=True)

    async def _test_spawn(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None or self.guild_id is None:
            await interaction.followup.send(text("encounter.server_only"), ephemeral=True)
            return
        if not await user_can_manage_guild(guild, interaction.user.id):
            await interaction.followup.send(text("setup.manage_server"), ephemeral=True)
            return
        channel = next((guild.get_channel(c) for c in self.setup.spawn_channel_ids if guild.get_channel(c)), None)
        if channel is None:
            await interaction.followup.send(text("setup.select_channel"), ephemeral=True)
            return
        try:
            await publish_spawn(channel, guild_id=guild.id, clone_id=self.clone_id, setup=self.setup, source="test")
        except Exception:
            logger.exception("Catch test spawn failed guild=%s channel=%s", guild.id, channel.id)
            await interaction.followup.send(text("setup.test_failed", channel=channel.mention), ephemeral=True)
            return
        await interaction.followup.send(text("setup.test_posted", channel=channel.mention), ephemeral=True)


class SpawnClaimView(discord.ui.View):
    def __init__(self, spawn_id: int):
        super().__init__(timeout=None)
        self.spawn_id = spawn_id
        self.ball = "capsule_basic"
        self.bait: str | None = None
        ball_select = discord.ui.Select(
            placeholder=text("ui.choose_capsule"),
            options=[
                discord.SelectOption(label=choice_label(key), value=key, default=key == self.ball)
                for key in ("capsule_basic", "capsule_sturdy", "capsule_prime", "capsule_sovereign")
            ],
            custom_id=f"catch:ball:{spawn_id}",
            row=0,
        )
        ball_select.callback = self._select_ball
        self.add_item(ball_select)
        bait_select = discord.ui.Select(
            placeholder=text("ui.optional_berry"),
            options=[
                discord.SelectOption(label=text("ui.no_berry"), value="none", default=True),
                discord.SelectOption(label=text("ui.honeyberry"), value="honeyberry"),
                discord.SelectOption(label=text("ui.goldberry"), value="goldberry"),
            ],
            custom_id=f"catch:bait:{spawn_id}",
            row=1,
        )
        bait_select.callback = self._select_bait
        self.add_item(bait_select)
        throw = discord.ui.Button(
            label=text("ui.throw_ball"),
            style=button_style("claim"),
            custom_id=f"catch:throw:{spawn_id}",
            row=2,
        )
        throw.callback = self._claim
        self.add_item(throw)

    async def _select_ball(self, interaction: discord.Interaction) -> None:
        self.ball = (interaction.data or {}).get("values", [self.ball])[0]
        await interaction.response.edit_message(view=self)

    async def _select_bait(self, interaction: discord.Interaction) -> None:
        value = (interaction.data or {}).get("values", ["none"])[0]
        self.bait = None if value == "none" else value
        await interaction.response.edit_message(view=self)

    async def _claim(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        gate = await check_player_allowed(
            interaction.user.id,
            interaction.guild_id,
            "catch",
            getattr(interaction.client, "clone_id", None),
        )
        if not gate.allowed:
            await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
            return
        try:
            await ensure_starter_kit(interaction.user.id, getattr(interaction.client, "clone_id", None))
            result = await record_catch(
                user_id=interaction.user.id,
                clone_id=getattr(interaction.client, "clone_id", None),
                guild_id=interaction.guild_id,
                source="wild",
                spawn_id=self.spawn_id,
                ball=self.ball,
                bait=self.bait,
            )
        except CatchBlocked as exc:
            await interaction.followup.send(text("claim.blocked", reason=exc.reason), ephemeral=True)
            return
        except ValueError as exc:
            if str(exc).endswith("is not available"):
                await interaction.followup.send(text("claim.no_items"), ephemeral=True)
                return
            logger.exception("Catch claim rejected spawn=%s user=%s", self.spawn_id, interaction.user.id)
            await interaction.followup.send(text("claim.error"), ephemeral=True)
            return
        except Exception:
            logger.exception("Catch claim failed spawn=%s user=%s", self.spawn_id, interaction.user.id)
            await interaction.followup.send(text("claim.error"), ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        if not result.claimed:
            embed = discord.Embed(
                title=text("claim.fled.title"),
                description=text("claim.fled.description"),
                colour=state_color("danger"),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        species = all_species().get(result.species_id)
        base_name = species["name"] if species else "Creature"
        name = f"Shiny {base_name}" if result.shiny else base_name
        embed = discord.Embed(
            title=text("claim.success.title"),
            description=text("claim.success.description", name=name),
            colour=state_color("success"),
        )
        embed.add_field(name="Level", value=str(result.level), inline=True)
        if result.new_species:
            embed.add_field(name="Dex", value=text("claim.new_species"), inline=True)
        if not getattr(result, "replay", False) and getattr(result, "owned_id", None) is not None:
            await self._add_buddy_xp(embed, interaction, result)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _add_buddy_xp(self, embed: discord.Embed, interaction: discord.Interaction, result) -> None:
        """Best effort: a buddy XP problem must never undo or hide a finished catch."""
        try:
            xp = await grant_buddy_catch_xp(
                interaction.user.id,
                getattr(interaction.client, "clone_id", None),
                caught_owned_id=result.owned_id,
                new_species=result.new_species,
                guild_id=interaction.guild_id,
            )
        except Exception:
            logger.exception("Buddy xp failed owned=%s user=%s", result.owned_id, interaction.user.id)
            return
        if xp is None or not xp.ok or xp.gained <= 0:
            return
        key = "xp.buddy_level_up" if xp.leveled else "xp.buddy_gain"
        embed.add_field(
            name=text("xp.field"),
            value=text(key, gained=xp.gained, level=xp.level_after),
            inline=False,
        )


class CatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._trigger_states: dict[tuple[int, int | None], ChannelTriggerState] = {}
        self._scheduler.start()

    def cog_unload(self) -> None:
        self._scheduler.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        gate = await check_player_allowed(
            message.author.id,
            message.guild.id,
            "spawn",
            getattr(self.bot, "clone_id", None),
        )
        if not gate.allowed:
            return
        setup = await load_setup(message.guild.id, getattr(self.bot, "clone_id", None))
        key = (message.guild.id, message.channel.id)
        state = self._trigger_states.setdefault(key, ChannelTriggerState())
        decision = consider_message(
            setup,
            state,
            channel_id=message.channel.id,
            user_id=message.author.id,
            content=message.content,
            is_bot=message.author.bot,
        )
        if not decision.should_spawn:
            return
        try:
            await publish_spawn(
                message.channel,
                guild_id=message.guild.id,
                clone_id=getattr(self.bot, "clone_id", None),
                setup=setup,
            )
        except Exception:
            state.message_count = 0
            logger.exception(
                "Catch spawn publish failed guild=%s channel=%s user=%s",
                message.guild.id,
                message.channel.id,
                message.author.id,
            )
            return

    @tasks.loop(seconds=30)
    async def _scheduler(self) -> None:
        try:
            await self._process_scheduler_batch()
        except Exception:
            logger.exception("Catch scheduler tick failed")

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _mark_spawn_expired(self, row: dict) -> None:
        channel_id = row.get("channel_id")
        message_id = row.get("message_id")
        spawn_id = row.get("id")
        if not channel_id or not message_id or not spawn_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.DiscordException:
                return
        try:
            message = await channel.fetch_message(int(message_id))
            view = SpawnClaimView(int(spawn_id))
            for item in view.children:
                item.disabled = True
            embed = discord.Embed(
                title=text("claim.fled.title"),
                description=text("claim.fled.description"),
                colour=state_color("warning"),
            )
            await message.edit(embed=embed, view=view)
        except discord.DiscordException:
            return

    async def _deliver_reminder(self, reminder: Reminder) -> bool:
        content = str(reminder.payload.get("content", text("reminder.default")))[:2000]
        allowed_mentions = discord.AllowedMentions.none()
        try:
            if reminder.delivery == "channel" and reminder.channel_id:
                channel = self.bot.get_channel(reminder.channel_id) or await self.bot.fetch_channel(reminder.channel_id)
                await channel.send(content, allowed_mentions=allowed_mentions)
                return True
            user = self.bot.get_user(reminder.user_id) or await self.bot.fetch_user(reminder.user_id)
            await user.send(content, allowed_mentions=allowed_mentions)
            return True
        except (discord.DiscordException, AttributeError):
            logger.exception("Catch reminder delivery failed reminder=%s user=%s", reminder.id, reminder.user_id)
            return False

    async def _process_scheduler_batch(self) -> None:
        batch = await run_scheduler_batch()
        for row in batch.expired_rows:
            try:
                await self._mark_spawn_expired(row)
            except Exception:
                logger.exception("Catch spawn expiry handling failed spawn=%s", row.get("id"))
        await dispatch_due_reminders(self._deliver_reminder)

    @app_commands.command(name="catch", description=text("command.description"))
    async def catch(self, interaction: discord.Interaction) -> None:
        # Acknowledge first (3-second rule); the gate reads feature flags from the DB.
        await interaction.response.defer(ephemeral=True)
        gate = await check_player_allowed(
            interaction.user.id,
            interaction.guild_id,
            "view",
            getattr(interaction.client, "clone_id", None),
        )
        if not gate.allowed:
            await interaction.followup.send(
                text("unavailable", reason=gate.reason or "disabled"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=build_hub_embed(), view=CatchHubView(), ephemeral=True)


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
