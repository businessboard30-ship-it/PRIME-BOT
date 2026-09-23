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
"""

import logging
from typing import List, Optional, Tuple

import discord

from database import db, get_pool
from modules.ai_command_allowlist import is_command_allowed, AI_COMMANDS, AICommandSpec
from modules.superbot_adapter import get_user_tier

logger = logging.getLogger(__name__)


class AICommandDenied(Exception):
    """Raised by execute_ai_command; the caller shows str(exc) to the user
    verbatim UNLESS exc.cap_reached is True, in which case the caller
    should call send_cap_reached_prompt(interaction) instead so the user
    gets the real Go Premium button, not just text."""

    def __init__(self, message: str, cap_reached: bool = False):
        super().__init__(message)
        self.cap_reached = cap_reached


# Daily cap on AI-EXECUTED commands (kick/ban/timeout/etc via natural
# language), separate from AI_USAGE_CAPS in modules/ai_features.py (which
# caps /aichat messages and /aiimage generations — a different, unrelated
# resource). Deliberately much lower than the chat caps: these commands
# mutate real server/member state, so even a "generous" tier shouldn't be
# generous here. Same tier keys as AI_USAGE_CAPS/get_user_tier so this
# reads consistently with the rest of the AI feature set.
AI_COMMAND_DAILY_CAPS = {
    "basic": 5,
    "pro": 20,
    "elite": 50,
    "founder": 200,
}


async def _tier(user_id: int) -> str:
    tier = await get_user_tier(user_id)
    return tier if tier in AI_COMMAND_DAILY_CAPS else "basic"


async def get_user_ai_command_usage(user_id: int) -> int:
    """Count of ALLOWED (not denied) AI-executed commands today for this
    user, across every guild — same "per user, resets midnight UTC" shape
    as get_user_ai_usage in ai_features.py. Denied attempts don't count
    against the cap; there's nothing to ration if nothing ran."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM ai_command_log WHERE user_id = $1 "
            "AND allowed = TRUE AND DATE(created_at) = (NOW() AT TIME ZONE 'UTC')::date",
            user_id,
        )
    return count or 0


async def check_ai_command_cap(user_id: int) -> Tuple[bool, Optional[str]]:
    """Returns (allowed, denial_reason). Call this BEFORE execute_ai_command
    (resolve_and_check already does) — never after, since the point is to
    refuse before anything runs, not to undo something that already ran."""
    tier = await _tier(user_id)
    cap = AI_COMMAND_DAILY_CAPS[tier]
    used = await get_user_ai_command_usage(user_id)
    if used >= cap:
        return False, (
            f"You've hit today's limit of {cap} AI-run actions on the **{tier}** tier — "
            f"resets at midnight UTC, or go Premium for a higher limit."
        )
    return True, None


async def send_cap_reached_prompt(interaction: discord.Interaction) -> None:
    """Call this (instead of just showing check_ai_command_cap's text reason)
    when the denial the user is hitting is SPECIFICALLY the daily AI-command
    cap — not a permission or allowlist denial, which should just show their
    plain text reason. This sends the real 'Go Premium' pitch — the exact
    same discord_bot.cogs._views_premium.send_premium_pitch used by the
    join-DM and the renewal-reminder DM — with its live Subscribe button
    wired to payments_manual.start_dual_mode_payment (Paystack/Gumroad).
    It is NOT a static "go buy premium" message; tapping it starts a real
    checkout. This raises the guild's premium status, not the caller's AI
    tier — there's no credit-purchase path here, by design (see PRIME-BOT's
    own note: the AI command cap resets on tier or on the clock, never on
    a one-off credit buy).

    Precondition: interaction.response must already be deferred (ephemeral),
    same precondition send_premium_pitch itself documents — the caller
    (whatever NLU/chat cog eventually invokes resolve_and_check) is
    responsible for that, same as it would be for any other followup.
    """
    if interaction.guild_id is None:
        return
    from discord_bot.cogs._views_premium import send_premium_pitch
    tier = await _tier(interaction.user.id)
    cap = AI_COMMAND_DAILY_CAPS[tier]
    await interaction.followup.send(
        f"You've hit today's limit of {cap} AI-run actions on the **{tier}** tier "
        f"(resets at midnight UTC). Go Premium for this server to raise it:",
        ephemeral=True,
    )
    clone_id = getattr(interaction.client, "clone_id", None)
    await send_premium_pitch(interaction, interaction.guild_id, clone_id)


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


async def resolve_and_check(interaction: discord.Interaction, command_name: str, args: dict) -> Tuple[Optional[AICommandSpec], Optional[discord.app_commands.Command], Optional[str], bool]:
    """Layer 1 + 2 (+ the daily cap) only — no execution, no confirmation.
    Use this to decide whether to even show a confirm prompt. Returns
    (spec, command, denial_reason, cap_reached); denial_reason is None iff
    both spec and command are non-None. cap_reached is True only when the
    reason for denial is SPECIFICALLY the daily AI-command cap (not a
    permission/allowlist denial) — the caller should respond to that case
    by calling send_cap_reached_prompt(interaction) for the real Premium
    button, not by just showing denial_reason as plain text."""
    spec = is_command_allowed(command_name)
    if spec is None:
        reason = f"'{command_name}' isn't something I'm able to run."
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason, False

    command = _resolve_command(interaction.client, command_name)
    if command is None:
        reason = f"'{command_name}' is allowlisted but I couldn't find the live command — probably a naming drift, tell the bot owner."
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason, False

    ok, reason = await check_real_permission(interaction, command)
    if not ok:
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason, False

    ok, reason = await check_ai_command_cap(interaction.user.id)
    if not ok:
        await db.log_ai_command(interaction.guild_id, interaction.user.id, command_name, args, allowed=False, denial_reason=reason)
        return None, None, reason, True

    return spec, command, None, False


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

    Also returns nothing at all if the user has already hit their daily
    AI-command cap (see AI_COMMAND_DAILY_CAPS / check_ai_command_cap) —
    no point offering commands the very next call will refuse anyway.

    No result is logged here — this is a visibility filter, not an
    attempt, so it would just be noise in ai_command_log.
    """
    qualifying: List[AICommandSpec] = []
    if interaction.guild is None:
        return qualifying
    cap_ok, _reason = await check_ai_command_cap(interaction.user.id)
    if not cap_ok:
        return qualifying
    for spec in AI_COMMANDS:
        command = _resolve_command(interaction.client, spec.name)
        if command is None:
            continue
        ok, _reason = await check_real_permission(interaction, command)
        if ok:
            qualifying.append(spec)
    return qualifying


async def execute_ai_command(interaction: discord.Interaction, command_name: str, **kwargs) -> None:
    """Call ONLY after: (1) resolve_and_check returned no denial_reason, and
    (2) if spec.requires_confirmation, the user has explicitly confirmed.
    Raises AICommandDenied if anything is off — including being called
    without a fresh re-check, since permissions can change between a
    confirm prompt being shown and being clicked. If exc.cap_reached is
    True, catch it and call send_cap_reached_prompt(interaction) instead
    of just showing str(exc) — that's what renders the actual Premium
    button rather than plain text."""
    spec, command, reason, cap_reached = await resolve_and_check(interaction, command_name, kwargs)
    if spec is None:
        raise AICommandDenied(reason, cap_reached=cap_reached)

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
