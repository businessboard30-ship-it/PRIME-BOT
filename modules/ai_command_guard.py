# path: modules/ai_command_guard.py

"""
Runtime enforcement for AI-triggered command execution. See
modules/ai_command_allowlist.py for what's allowed and why.

Three layers, all must pass, in this order:
  1. is_command_allowed(name) — is this command even eligible? (allowlist +
     money-module guard + owner-only guard, all in one place)
  2. check_real_permission() — does THIS user, in THIS guild, actually have
     the Discord permission the real slash command itself requires? This
     reads the command's own registered checks (app_commands.checks.*,
     guild_only, etc.) directly off the command object via bot.tree — it
     does not reimplement or guess at what each command needs, so the AI
     path can never drift out of sync with what the manual command allows.
  3. requires_confirmation — if True, the caller must have already gotten
     an explicit confirm click (see AIConfirmView) before execute_ai_command
     is called at all; this module doesn't render UI itself.

Every attempt — allowed or denied — is logged via db.log_ai_command.

get_qualifying_commands() adds a fourth, EARLIER step: filtering what the
AI is even offered as an option, before the user asks for anything and
before a confirm button could ever be shown. See its docstring below.
"""

import logging
from typing import List, Optional, Tuple

import discord

from database import db
from modules.ai_command_allowlist import is_command_allowed, AI_COMMANDS, AICommandSpec

logger = logging.getLogger(__name__)


class AICommandDenied(Exception):
    """Raised by execute_ai_command; the caller shows str(exc) to the user
    verbatim — it's always already a safe, user-facing message."""


async def check_real_permission(interaction: discord.Interaction, command: discord.app_commands.Command) -> Tuple[bool, Optional[str]]:
    """Runs the actual registered checks on `command` (the real
    discord.app_commands.Command object, looked up from the bot's own
    command tree) against this interaction. Returns (ok, reason_if_not).

    This deliberately does NOT special-case permission names — whatever
    checks the command decorated itself with (has_permissions, guild_only,
    a custom predicate) are exactly what runs here, unmodified."""
    if interaction.guild is None:
        return False, "This can only be used inside a server."
    for check in command.checks:
        try:
            ok = await discord.utils.maybe_coroutine(check, interaction)
        except discord.app_commands.AppCommandError as e:
            return False, str(e) or "You don't have permission to do that."
        except Exception as e:
            logger.warning(f"[ai_command_guard] check {check!r} raised unexpectedly: {e}")
            return False, "Couldn't verify your permissions for that."
        if not ok:
            return False, "You don't have permission to do that."
    return True, None


def _resolve_command(bot: discord.Client, name: str) -> Optional[discord.app_commands.Command]:
    cmd = bot.tree.get_command(name)
    if cmd is not None:
        return cmd
    # Fall back to a full walk for commands nested in a Group, if any ever
    # get added to the allowlist — get_command only finds top-level ones.
    for cmd in bot.tree.walk_commands():
        if cmd.name == name:
            return cmd
    return None


async def get_qualifying_commands(interaction: discord.Interaction) -> List[AICommandSpec]:
    """Pre-filter, run BEFORE the user ever asks for anything and BEFORE
    the AI's tool schema for this turn is built. Walks the full
    allowlist and keeps only the specs whose real command this user
    actually passes check_real_permission() for right now, in this guild.

    Callers (the NLU/tool-calling layer) should build the model's
    available-tools list from this, not from AI_COMMANDS directly — a
    command a user doesn't qualify for should never reach the model as
    an option, so there's nothing to refuse and no confirm button to
    show for it. This does NOT replace resolve_and_check/execute_ai_command
    — permissions can change between this call and the moment a command
    is actually invoked (roles edited mid-conversation, etc.), so the
    same real check still runs again right before execution.

    No result is logged here — this is a visibility filter, not an
    attempt, so it would just be noise in ai_command_log.
    """
    qualifying: List[AICommandSpec] = []
    if interaction.guild is None:
        return qualifying
    for spec in AI_COMMANDS:
        command = _resolve_command(interaction.client, spec.name)
        if command is None:
            continue
        ok, _reason = await check_real_permission(interaction, command)
        if ok:
            qualifying.append(spec)
    return qualifying


async def resolve_and_check(interaction: discord.Interaction, command_name: str, args: dict) -> Tuple[Optional[AICommandSpec], Optional[discord.app_commands.Command], Optional[str]]:
    """Layer 1 + 2 only — no execution, no confirmation. Use this to decide
    whether to even show a confirm prompt. Returns (spec, command, denial_reason);
    denial_reason is None iff both spec and command are non-None."""
    spec = is_command_allowed(command_name)
    if spec is None:
        reason = f"'{command_name}' isn't something I'm able to run."
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason

    command = _resolve_command(interaction.client, command_name)
    if command is None:
        reason = f"'{command_name}' is allowlisted but I couldn't find the live command — probably a naming drift, tell the bot owner."
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason

    ok, reason = await check_real_permission(interaction, command)
    if not ok:
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason

    return spec, command, None


async def execute_ai_command(interaction: discord.Interaction, command_name: str, **kwargs) -> None:
    """Call ONLY after: (1) resolve_and_check returned no denial_reason, and
    (2) if spec.requires_confirmation, the user has explicitly confirmed.
    Raises AICommandDenied if anything is off — including being called
    without a fresh re-check, since permissions can change between a
    confirm prompt being shown and being clicked."""
    spec, command, reason = await resolve_and_check(interaction, command_name, kwargs)
    if spec is None:
        raise AICommandDenied(reason)

    await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, kwargs, allowed=True)
    # Binds and calls the command's own callback directly through discord.py's
    # normal invocation path (parameter transformation, cooldowns, the works)
    # rather than hand-rolling argument passing — same machinery a real
    # interaction uses.
    await command._do_call(interaction, kwargs)


class AIConfirmView(discord.ui.View):
    """Shown before executing anything with requires_confirmation=True.
    `on_confirm` is an async callback taking the interaction; this view
    doesn't know how to run the command itself, just gates the click."""

    def __init__(self, invoker_id: int, on_confirm, *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing was run.", view=self)
        self.stop()
