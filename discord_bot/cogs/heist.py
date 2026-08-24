"""
Heist Wars — Discord presentation layer only.

No success-chance math, no reward math, no state-machine logic lives here —
all of that is game/heist_engine.py, game/decision_engine.py,
game/reward_engine.py, game/heist_service.py, game/item_engine.py and
game/item_service.py. This cog's job is: slash commands, embeds, buttons,
and translating service-layer exceptions into user-facing messages.

Ownership: buttons carry run_id in their custom_id for routing, but every
callback re-derives the acting user from `interaction.user.id` (never from
the custom_id) and heist_service re-validates that against the DB-stored
owner before doing anything — a modified custom_id cannot act on someone
else's run (brief §10-11).

Visual identity lives in discord_bot/cogs/_heist_ui.py — every embed here
is built through those shared helpers so the game reads as one product.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs import _heist_ui as ui
from discord_bot.cogs._dm_support import GuildOnlyCog
from game import heist_service, item_service
from game.items import get_item
from game.locations import LOCATIONS, get_location
from game.models import Approach, HeistState

logger = logging.getLogger(__name__)

STATE_LABELS = {
    HeistState.PLANNING: "Planning",
    HeistState.INFILTRATION: "Infiltration",
    HeistState.OBJECTIVE: "Objective",
    HeistState.LOOT: "Loot",
    HeistState.ESCAPE: "Escape",
    HeistState.COMPLETED: "Operation Complete",
    HeistState.FAILED: "Operation Compromised",
    HeistState.EXPIRED: "Expired",
}

APPROACH_INFO = {
    Approach.STEALTH: ("Stealth Protocol", "Low visibility. Requires precision.", "+8%", "LOW", ui.GREEN),
    Approach.TECHNICAL: ("Technical Protocol", "Adaptive, gadget-driven approach.", "+4%", "MEDIUM", ui.CYAN),
    Approach.LOUD: ("Force Protocol", "Maximum pressure. High volatility.", "-5%", "HIGH", ui.RED),
}


def _clone_id_for(interaction: discord.Interaction) -> Optional[int]:
    return getattr(interaction.client, "clone_id", None)


class LocationSelect(discord.ui.Select):
    def __init__(self, cog: "HeistCog"):
        options = [
            discord.SelectOption(
                label=f"{loc.name} — Lv.{loc.required_level}+",
                value=loc.key,
                description=f"{loc.base_chance}% base • {ui.money(loc.min_reward_cash)}-{ui.money(loc.max_reward_cash)}",
                emoji="🔷",
            )
            for loc in LOCATIONS.values()
        ]
        super().__init__(placeholder="Select a target...", options=options, min_values=1, max_values=1)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_location_chosen(interaction, self.values[0])


class LocationView(discord.ui.View):
    def __init__(self, cog: "HeistCog"):
        super().__init__(timeout=120)
        self.add_item(LocationSelect(cog))


class ApproachView(discord.ui.View):
    def __init__(self, cog: "HeistCog", location_key: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.location_key = location_key
        for approach in (Approach.STEALTH, Approach.TECHNICAL, Approach.LOUD):
            self.add_item(self._make_button(approach))

    def _make_button(self, approach: Approach):
        view = self
        label, _, _, _, color = APPROACH_INFO[approach]
        style = {
            ui.GREEN: discord.ButtonStyle.success,
            ui.CYAN: discord.ButtonStyle.primary,
            ui.RED: discord.ButtonStyle.danger,
        }[color]

        class _Btn(discord.ui.Button):
            def __init__(self):
                super().__init__(label=label.replace(" Protocol", "").upper(), style=style)

            async def callback(self, interaction: discord.Interaction):
                await view.cog.handle_approach_chosen(interaction, view.location_key, approach)

        return _Btn()


class EventChoiceView(discord.ui.View):
    def __init__(self, cog: "HeistCog", run_id: int, choices: tuple):
        super().__init__(timeout=180)
        self.cog = cog
        self.run_id = run_id
        for choice in choices:
            self.add_item(self._make_button(choice))

    def _make_button(self, choice):
        view = self

        class _Btn(discord.ui.Button):
            def __init__(self):
                super().__init__(label=choice.label, style=discord.ButtonStyle.primary,
                                  custom_id=f"heist:choice:{view.run_id}:{choice.key}")

            async def callback(self, interaction: discord.Interaction):
                await view.cog.handle_choice(interaction, view.run_id, choice.key)

        return _Btn()


class HeistCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- embed builders -----------------------------------------------------

    def _location_embed(self, location_key: str) -> discord.Embed:
        loc = get_location(location_key)
        threat_pct = min(100, loc.difficulty_penalty * 2)
        security_pct = max(0, 100 - loc.base_chance)
        threat_txt, threat_color, threat_glyph = ui.risk_label(threat_pct)
        embed = ui.base_embed(f"Target // {loc.name}", color=threat_color,
                               description="This is a dangerous operation." if threat_pct >= 70 else None)
        embed.add_field(name="THREAT", value=f"{ui.progress_bar(threat_pct)} {threat_glyph} {threat_txt}", inline=False)
        embed.add_field(name="SECURITY", value=ui.progress_bar(security_pct), inline=False)
        embed.add_field(name="EST. PAYOUT", value=f"{ui.money(loc.min_reward_cash)} — {ui.money(loc.max_reward_cash)}", inline=True)
        embed.add_field(name="REQUIRED LEVEL", value=str(loc.required_level), inline=True)
        embed.add_field(name="COOLDOWN", value=ui.fmt_minutes(loc.cooldown_seconds), inline=True)
        embed.set_footer(text="Select an approach to begin the operation.")
        return embed

    def _approach_embed(self, location_key: str) -> discord.Embed:
        loc = get_location(location_key)
        embed = ui.base_embed("Loadout // Approach Selection", color=ui.CYAN,
                               description=f"Target: **{loc.name}**")
        for approach in (Approach.STEALTH, Approach.TECHNICAL, Approach.LOUD):
            name, desc, modifier, risk, color = APPROACH_INFO[approach]
            embed.add_field(
                name=f"◈ {name.upper()}",
                value=f"{desc}\nSUCCESS MODIFIER: **{modifier}**\nRISK: **{risk}**",
                inline=True,
            )
        return embed

    async def _equipment_lines(self, guild_id: int, clone_id: Optional[int], user_id: int, phase: HeistState) -> str:
        equipped = await item_service.equipped_gameplay_item_keys(guild_id, clone_id, user_id)
        if not equipped:
            return "None equipped — see `/loadout`."
        lines = []
        for key in equipped:
            item = get_item(key)
            if not item:
                continue
            relevant = any(eff.phase == phase for eff in item.effects if eff.phase is not None)
            marker = "✓" if relevant else "●"
            lines.append(f"{marker} {item.name}")
        return "\n".join(lines) if lines else "None equipped — see `/loadout`."

    async def _render_event_embed(self, interaction: discord.Interaction, run: dict) -> tuple[discord.Embed, discord.ui.View | None]:
        location = get_location(run["location_key"])
        state = HeistState(run["status"])
        guild_id, clone_id, user_id = run["guild_id"], run["clone_id"], run["user_id"]

        if state in (HeistState.INFILTRATION, HeistState.OBJECTIVE, HeistState.LOOT):
            current = await heist_service.current_event_for_run(run)
            embed = ui.base_embed(f"Operation Active // {location.name}", color=ui.CYAN)
            embed.add_field(name="PHASE", value=STATE_LABELS[state], inline=True)
            embed.add_field(name="APPROACH", value=run["approach"].title(), inline=True)
            phase_index = {HeistState.INFILTRATION: 1, HeistState.OBJECTIVE: 2, HeistState.LOOT: 3}[state]
            embed.add_field(name="PROGRESS", value=ui.progress_bar(phase_index * 33), inline=False)

            equip_lines = await self._equipment_lines(guild_id, clone_id, user_id, state)
            embed.add_field(name="EQUIPMENT", value=equip_lines, inline=False)

            if not current:
                embed.add_field(name="STATUS", value="Resolving...", inline=False)
                return embed, None
            event = current["event"]
            embed.add_field(name="⚠ SECURITY EVENT", value=event.description, inline=False)
            view = EventChoiceView(self, run["id"], event.choices)
            return embed, view

        if state == HeistState.COMPLETED:
            embed = ui.base_embed(f"Operation Complete // {location.name}", color=ui.GREEN,
                                   description="✓ **SUCCESS** — the crew got out clean.")
            embed.add_field(name="CASH", value=f"+{ui.money(run['reward_cash'])}", inline=True)
            embed.add_field(name="XP", value=f"+{run['reward_xp']}", inline=True)
            embed.add_field(name="INTEL", value=f"+{run['reward_intel']}", inline=True)
            embed.add_field(name="REPUTATION", value=f"+{run['reward_reputation']}", inline=True)
            dropped_key = run.get("dropped_item_key")
            if dropped_key:
                item = get_item(dropped_key)
                if item:
                    embed.add_field(name="ITEM ACQUIRED", value=ui.item_line(item), inline=False)
                    embed.color = ui.RARITY_COLOR[item.rarity]
            embed.add_field(name="NEXT OPERATION AVAILABLE IN", value=ui.fmt_minutes(location.cooldown_seconds), inline=False)
            return embed, None

        if state == HeistState.FAILED:
            embed = ui.base_embed(f"Operation Compromised // {location.name}", color=ui.RED,
                                   description="✕ Security response escalated. The crew extracted before capture.")
            if run.get("reward_cash") is not None:
                embed.add_field(name="CASH", value=f"+{ui.money(run['reward_cash'])}", inline=True)
                embed.add_field(name="XP", value=f"+{run['reward_xp']}", inline=True)
            embed.add_field(name="COOLDOWN", value=ui.fmt_minutes(location.cooldown_seconds), inline=False)
            return embed, None

        embed = ui.base_embed(f"{location.name}", color=ui.GRAY, description="...")
        return embed, None

    async def _send_error(self, interaction: discord.Interaction, message: str):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(f"⚠ {message}", ephemeral=True)

    # -- slash command --------------------------------------------------------

    @app_commands.command(name="heist", description="Open the Heist Wars operations console")
    async def heist(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        clone_id = _clone_id_for(interaction)
        user_id = interaction.user.id

        active = await heist_service.resume_heist(guild_id, clone_id, user_id)
        if active:
            embed, view = await self._render_event_embed(interaction, active)
            embed.set_footer(text="Resumed active operation.")
            await interaction.response.send_message(embed=embed, view=view or discord.utils.MISSING, ephemeral=True)
            return

        embed = ui.base_embed(
            "Nova City // Heist Control System", color=ui.CYAN,
            description="Choose a target from the intelligence network to begin.",
        )
        await interaction.response.send_message(embed=embed, view=LocationView(self), ephemeral=True)

    # -- flow handlers (called by views, never directly by app_commands) ------

    async def handle_location_chosen(self, interaction: discord.Interaction, location_key: str):
        embed = self._approach_embed(location_key)
        await interaction.response.edit_message(embed=embed, view=ApproachView(self, location_key))

    async def handle_approach_chosen(self, interaction: discord.Interaction, location_key: str, approach: Approach):
        guild_id = interaction.guild_id
        clone_id = _clone_id_for(interaction)
        user_id = interaction.user.id
        try:
            run = await heist_service.start_heist(guild_id, clone_id, user_id, location_key, approach.value)
        except heist_service.LevelTooLowError:
            await interaction.response.edit_message(content="✕ Your level is too low for that location.", embed=None, view=None)
            return
        except heist_service.AlreadyActiveRunError:
            await interaction.response.edit_message(content="⚠ You already have an active operation. Use `/heist` to resume it.", embed=None, view=None)
            return
        except heist_service.CooldownActiveError as e:
            await interaction.response.edit_message(content=f"⚠ That target is on cooldown for {ui.fmt_minutes(e.retry_after_seconds)}.", embed=None, view=None)
            return
        except heist_service.HeistServiceError as e:
            await interaction.response.edit_message(content=f"✕ Couldn't start operation: {e}", embed=None, view=None)
            return

        embed, view = await self._render_event_embed(interaction, run)
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_choice(self, interaction: discord.Interaction, run_id: int, choice_key: str):
        guild_id = interaction.guild_id
        clone_id = _clone_id_for(interaction)
        user_id = interaction.user.id
        try:
            run = await heist_service.choose_event(guild_id, clone_id, user_id, run_id, choice_key)
        except heist_service.NotOwnerError:
            await self._send_error(interaction, "This isn't your operation.")
            return
        except (heist_service.NoActiveRunError, heist_service.InvalidStateError):
            await self._send_error(interaction, "This operation is no longer active.")
            return
        except heist_service.HeistServiceError as e:
            await self._send_error(interaction, str(e))
            return

        embed, view = await self._render_event_embed(interaction, run)
        await interaction.response.edit_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HeistCog(bot))
