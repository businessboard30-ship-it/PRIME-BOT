"""/admin envsetup: the main bot DMs the owner a Components V2 form for the settings every bot shares.

Values are saved to the database (modules/owner_env.py) instead of the Railway Variables tab. Every bot reads them on
its next start. Owner only, main bot only, and secrets are never shown back: the form says "set (hidden)".
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional

import discord
from discord.ext import commands

from config import ADMIN_ID, DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs._admin_mount import mount_admin_command
from modules import owner_env as oe

logger = logging.getLogger(__name__)

ACCENT = discord.Colour(0x6E7B96)   # soft slate: no neon, no glow
_TIMEOUT = 1800


def _owner_ids() -> set:
    return ({ADMIN_ID} if ADMIN_ID else set()) | set(DISCORD_CLONE_ADMIN_IDS)


async def _deny(interaction: discord.Interaction) -> None:
    msg = "Only the bot owner can use this."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _counts(group: str, saved: Dict[str, Optional[str]]) -> tuple:
    specs = oe.group_specs(group)
    have = sum(1 for s in specs if saved.get(s.name) or (s.name not in saved and os.environ.get(s.name)))
    return have, len(specs)


def _needs_restart(saved: Dict[str, Optional[str]]) -> bool:
    return any(oe.status(s.name, saved)[1] for s in oe.SPECS)


# ── views ───────────────────────────────────────────────────────────────

class _OwnerView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=_TIMEOUT)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in _owner_ids():
            return True
        await _deny(interaction)
        return False


class GroupSelect(discord.ui.Select):
    def __init__(self, saved: Dict[str, Optional[str]]):
        opts = []
        for g in oe.GROUPS:
            have, total = _counts(g, saved)
            opts.append(discord.SelectOption(label=g, value=g, description=f"{have} of {total} set"))
        super().__init__(placeholder="Pick a group to fill in", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        saved = await oe.load_all()
        await interaction.edit_original_response(view=build_group(self.values[0], saved))


class RestartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Restart bots", style=discord.ButtonStyle.secondary,
                         emoji="\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(view=build_confirm())


class ConfirmRestartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Restart now", style=discord.ButtonStyle.danger,
                         emoji="\N{HIGH VOLTAGE SIGN}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await oe.request_restart()
        except Exception:
            logger.exception("[owner_env] restart request failed")
            return await interaction.edit_original_response(
                view=build_home(await oe.load_all(), notice="Couldn't ask for a restart, try again."))
        v = _OwnerView()
        c = discord.ui.Container(accent_colour=ACCENT)
        c.add_item(discord.ui.TextDisplay("## Restarting\nThe bots are coming back in about a minute. "
                                          "Saved values are live after that."))
        v.add_item(c)
        await interaction.edit_original_response(view=v)
        logger.warning("[owner_env] restart requested by owner %s", interaction.user.id)
        await asyncio.sleep(1.5)
        os._exit(1)   # non-zero exit: Railway starts the service again


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(view=build_home(await oe.load_all()))


def build_confirm() -> discord.ui.LayoutView:
    view = _OwnerView()
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(discord.ui.TextDisplay(
        "## Restart the bots?\nThe main bot, the clone manager and the API restart so saved values go live. "
        "Everything is back in about a minute."))
    row = discord.ui.ActionRow()
    row.add_item(ConfirmRestartButton())
    row.add_item(CancelButton())
    c.add_item(row)
    view.add_item(c)
    return view


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, emoji="\N{LEFTWARDS ARROW WITH HOOK}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(view=build_home(await oe.load_all()))


class FillButton(discord.ui.Button):
    def __init__(self, group: str, page: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.group, self.page = group, page

    async def callback(self, interaction: discord.Interaction):
        saved = await oe.load_all()
        await interaction.response.send_modal(FillModal(self.group, self.page, saved))


class ResetSelect(discord.ui.Select):
    def __init__(self, group: str, saved: Dict[str, Optional[str]]):
        opts = [discord.SelectOption(label=s.label[:100], value=s.name, description=s.name)
                for s in oe.group_specs(group) if saved.get(s.name)]
        super().__init__(placeholder="Reset a saved value (Railway applies again)", options=opts[:25],
                         min_values=1, max_values=1)
        self.group = group

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        name = self.values[0]
        await oe.reset(name)
        logger.info("[owner_env] %s reset by owner", name)
        await oe.refresh_live(force=True)
        saved = await oe.load_all()
        await interaction.edit_original_response(
            view=build_group(self.group, saved, notice=f"Reset **{oe.BY_NAME[name].label}**."))


def build_home(saved: Dict[str, Optional[str]], notice: Optional[str] = None) -> discord.ui.LayoutView:
    view = _OwnerView()
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(discord.ui.TextDisplay(
        "## Shared bot settings\n"
        "Fill these in once and every bot uses them after its next restart, so you don't have to touch Railway.\n"
        "A value saved here wins over the Railway one. Secrets are encrypted and never shown back."))
    c.add_item(discord.ui.Separator())
    lines = []
    for g in oe.GROUPS:
        have, total = _counts(g, saved)
        lines.append(f"**{g}** · {have} of {total} set")
    c.add_item(discord.ui.TextDisplay("\n".join(lines)))
    if notice:
        c.add_item(discord.ui.TextDisplay(notice))
    if _needs_restart(saved):
        c.add_item(discord.ui.TextDisplay("-# Some saved changes aren't live yet. Press Restart bots to apply them."))
    row = discord.ui.ActionRow()
    row.add_item(GroupSelect(saved))
    c.add_item(row)
    rrow = discord.ui.ActionRow()
    rrow.add_item(RestartButton())
    c.add_item(rrow)
    view.add_item(c)
    return view


def build_group(group: str, saved: Dict[str, Optional[str]], notice: Optional[str] = None) -> discord.ui.LayoutView:
    view = _OwnerView()
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(discord.ui.TextDisplay(f"## {group}"))
    c.add_item(discord.ui.Separator())
    body = []
    for s in oe.group_specs(group):
        state, restart = oe.status(s.name, saved)
        body.append(f"**{s.label}** · `{s.name}`\n-# {state}{' · restart needed' if restart else ''}")
    c.add_item(discord.ui.TextDisplay("\n".join(body)))
    if notice:
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(notice))
    c.add_item(discord.ui.TextDisplay(
        "-# In the form: leave a box empty to keep it, type `-` to reset it to the Railway value."))
    row = discord.ui.ActionRow()
    pages = oe.pages(group)
    for i, page in enumerate(pages):
        first = i * oe.PAGE + 1
        label = "Fill in" if len(pages) == 1 else f"Fill {first}-{first + len(page) - 1}"
        row.add_item(FillButton(group, i, label))
    row.add_item(BackButton())
    c.add_item(row)
    if any(saved.get(s.name) for s in oe.group_specs(group)):
        rrow = discord.ui.ActionRow()
        rrow.add_item(ResetSelect(group, saved))
        c.add_item(rrow)
    view.add_item(c)
    return view


class FillModal(discord.ui.Modal):
    def __init__(self, group: str, page: int, saved: Dict[str, Optional[str]]):
        super().__init__(title=f"{group}"[:45])
        self.group = group
        self.specs = oe.pages(group)[page]
        self.inputs: Dict[str, discord.ui.TextInput] = {}
        for s in self.specs:
            current = os.environ.get(s.name) if s.name not in saved else saved.get(s.name)
            ti = discord.ui.TextInput(
                required=False, max_length=oe.MAX_LEN,
                default=None if (s.secret or not current) else current[:oe.MAX_LEN],
                placeholder="hidden, type a new one to replace" if (s.secret and current) else "empty",
            )
            self.inputs[s.name] = ti
            self.add_item(discord.ui.Label(text=s.label[:45], component=ti))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in _owner_ids():
            return True
        await _deny(interaction)
        return False

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        before = await oe.load_all()
        saved_n = reset_n = 0
        problems = []
        for s in self.specs:
            raw = (self.inputs[s.name].value or "").strip()
            if not raw:
                continue
            if raw == "-":
                if before.get(s.name):
                    await oe.reset(s.name)
                    reset_n += 1
                    logger.info("[owner_env] %s reset by owner", s.name)
                continue
            current = before.get(s.name) if s.name in before else os.environ.get(s.name)
            if not s.secret and raw == current:
                continue   # prefilled value left as it was
            ok, value = oe.clean(s, raw)
            if not ok:
                problems.append(f"**{s.label}**: {value}")
                continue
            try:
                await oe.save(s.name, value)
            except Exception:
                logger.exception("[owner_env] saving %s failed", s.name)
                problems.append(f"**{s.label}**: couldn't save it, try again")
                continue
            saved_n += 1
            logger.info("[owner_env] %s saved by owner", s.name)   # the name only, never the value
        if saved_n or reset_n:
            await oe.refresh_live(force=True)   # Groq keys go live right here
        notes = []
        if saved_n:
            notes.append(f"Saved {saved_n}.")
        if reset_n:
            notes.append(f"Reset {reset_n}.")
        if not notes and not problems:
            notes.append("Nothing changed.")
        text = " ".join(notes)
        if problems:
            text += ("\n" if text else "") + "Not saved:\n" + "\n".join(f"- {p}" for p in problems)
        await interaction.edit_original_response(view=build_group(self.group, await oe.load_all(), notice=text))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception("[owner_env] form failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)


# ── command ─────────────────────────────────────────────────────────────

class OwnerEnvCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._watch = asyncio.create_task(oe.restart_watcher())
        mount_admin_command(
            self.bot, self.envsetup, name="envsetup",
            description="[Bot owner] DM me the form for settings all bots share",
        )

    async def cog_unload(self):
        self._watch.cancel()

    # Mounted as /admin envsetup (see cog_load).
    async def envsetup(self, interaction: discord.Interaction):
        if interaction.user.id not in _owner_ids():
            return await _deny(interaction)
        if getattr(interaction.client, "clone_id", None) is not None:
            return await interaction.response.send_message(
                "Run this on the main bot. Clones read the same saved settings.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            saved = await oe.load_all()
        except Exception:
            logger.exception("[owner_env] couldn't read saved settings")
            return await interaction.followup.send("Couldn't reach the database just now, try again.", ephemeral=True)
        view = build_home(saved)
        try:
            dm = await interaction.user.create_dm()
            await dm.send(view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't DM you (your DMs are closed to this server's bots). Here it is instead:", ephemeral=True)
            return await interaction.followup.send(view=view, ephemeral=True)
        await interaction.followup.send("Sent to your DMs.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerEnvCog(bot))
