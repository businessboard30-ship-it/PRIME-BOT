"""
Generic, reusable inline-keyboard building blocks shared by the smaller cogs
(ads_marketplace, ai_tools, automation, automod, clone_admin, crypto_alerts,
external_tools, leveling, premium, reaction_roles, welcome).

Rather than a bespoke `_views_<cog>.py` per file (as economy.py/moderation.py
got, since those needed custom multi-button flows like a Shop buy-modal),
these cogs' gaps were all the same shape: a "view/list a thing" command with
no way to refresh or jump to a related "view a thing" command without
retyping it. ActionButton + NavView cover that shape generically — each
button just calls back into a named method on the owning cog, same
no-duplicated-logic principle as everywhere else in this pass.

Discord's 5 button styles only (see _views_economy.py's note): primary
(blurple, recommended default per the audit's legend), secondary (grey,
navigation to something else), success (green, confirm/positive), danger
(red, destructive/clear).
"""

import discord


async def check_wizard_access(
    interaction: discord.Interaction,
    invoker_id,
    setup_command: str,
    permission: str = "manage_guild",
    permission_label: str = "Manage Server",
    admin_override: bool = False,
) -> bool:
    """Shared invoker/permission gate for the wizard dynamic items
    (automod, economy, community, giveaway, welcome, ticket, leveling).

    Each wizard file used to carry its own copy of this exact check,
    differing only in the setup command name quoted back to the user and
    (for leveling) which permission is required. Consolidated here so a
    future change to the access rule only needs to happen once.

    Two access rules exist across the wizards, controlled by admin_override:
    - False (automod, economy, community, welcome, ticket, leveling): when
      invoker_id is set, ONLY that user passes — permission is checked
      solely as a fallback when invoker_id is None.
    - True (giveaway): a user with `permission` always passes, even when
      invoker_id is set and doesn't match them — preserves giveaway's
      original admin-can-use-anyone's-wizard behavior.

    Always acknowledges the interaction (via response.send_message, or
    followup.send if the caller already deferred/responded) before
    returning False, so callers can safely treat `if not await
    check_wizard_access(...): return` as a complete guard clause with no
    risk of leaving the interaction un-acked OR raising InteractionResponded
    (several wizard buttons defer() before calling this, e.g. to do async
    work first — this must work regardless of that prior ack state).
    """
    has_permission = getattr(interaction.permissions, permission, False)

    async def _deny(msg: str):
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    if admin_override:
        if interaction.user.id == invoker_id or has_permission:
            return True
        await _deny(f"Only the person who ran /{setup_command} setup can use this.")
        return False

    if invoker_id is not None:
        if interaction.user.id != invoker_id:
            await _deny(f"Only the person who ran /{setup_command} setup can use this.")
            return False
        return True
    if not has_permission:
        await _deny(f"You need the **{permission_label}** permission to use this.")
        return False
    return True


class ActionButton(discord.ui.Button):
    """A button that calls `getattr(cog, method_name)(interaction, *args,
    **kwargs)` — the same coroutine the slash command itself calls, so a
    button press and retyping the command always produce the same result."""

    def __init__(self, label: str, style: discord.ButtonStyle, cog, method_name: str,
                 emoji: str = None, args: tuple = (), kwargs: dict = None):
        super().__init__(label=label, style=style, emoji=emoji)
        self.cog = cog
        self.method_name = method_name
        self.args = args
        self.kwargs = kwargs or {}

    async def callback(self, interaction: discord.Interaction):
        target = getattr(self.cog, self.method_name)
        # @app_commands.command-decorated cog methods resolve to a Command
        # object on the instance (not a bound coroutine) — self isn't bound
        # automatically, so call through .callback(cog, ...) instead. Plain
        # helper methods (e.g. economy.py's send_balance) are already bound
        # and callable directly.
        if isinstance(target, discord.app_commands.Command):
            await target.callback(self.cog, interaction, *self.args, **self.kwargs)
        else:
            await target(interaction, *self.args, **self.kwargs)


class NavCardView(discord.ui.LayoutView):
    """Components V2 counterpart to NavView — a colored Container card
    (header + body lines) with the same ActionButtons dropped into an
    ActionRow instead of stacked below a plain-text message. Shared by
    every cog that used NavView, so switching one over is a one-line
    swap (NavView(...) -> NavCardView(header, lines, accent, ...))."""

    def __init__(self, header: str, lines: list, accent: discord.Color, buttons: list = None, timeout: int = 180):
        super().__init__(timeout=timeout)
        text = discord.ui.TextDisplay("\n".join([f"### {header}", *lines]))
        children = [text]
        if buttons:
            row = discord.ui.ActionRow()
            for b in buttons:
                row.add_item(b)
            children += [discord.ui.Separator(), row]
        self.add_item(discord.ui.Container(*children, accent_colour=accent))


class NavView(discord.ui.View):
    """Throwaway nav aid (3-minute timeout, matches EconomyMenuView) built
    from a flat list of ActionButtons."""

    def __init__(self, buttons: list, timeout: int = 180):
        super().__init__(timeout=timeout)
        for b in buttons:
            self.add_item(b)


def refresh_button(cog, method_name: str, args: tuple = (), kwargs: dict = None) -> ActionButton:
    """The common case: a single primary 'Refresh' button re-running the
    same view command."""
    return ActionButton("Refresh", discord.ButtonStyle.primary, cog, method_name,
                         emoji="🔄", args=args, kwargs=kwargs)
