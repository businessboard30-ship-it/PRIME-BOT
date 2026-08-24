"""
Inline keyboards for ModerationCog.

Moderation actions are logged as they happen, so the highest-value button
here is a one-tap way to pull up the audit trail (/modlogs) or a target's
warn count (/warns) right after an action, without retyping either command.
Kept separate from moderation.py for the same reason as _views_economy.py.
"""

import discord


def get_mod_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("ModerationCog")


class ConfirmActionView(discord.ui.View):
    """Generic Confirm/Cancel gate for a destructive action.

    `on_confirm(interaction)` is awaited when Confirm is pressed — it does
    the actual work (ban/kick/clear warns) and is expected to edit the
    message itself via interaction.response.edit_message(...). Only the
    original invoker can press either button; anyone else gets a silent
    ephemeral nudge instead of the button just doing nothing.
    """

    def __init__(self, invoker_id: int, on_confirm, *, confirm_label: str = "Confirm", danger: bool = True):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.on_confirm = on_confirm
        self.confirm_btn.label = confirm_label
        self.confirm_btn.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can confirm it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled — no action taken.", view=self)


class ModActionView(discord.ui.View):
    """Attached to kick/ban/unban/timeout/untimeout responses."""

    def __init__(self, target_id: int):
        super().__init__(timeout=180)
        self.target_id = target_id

    @discord.ui.button(label="Mod Logs", style=discord.ButtonStyle.secondary, emoji="📋")
    async def modlogs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_modlogs(interaction, limit=10)


class WarnActionView(discord.ui.View):
    """Attached to /warn and /unwarn responses — jump straight to the
    target's current warn count or the full mod log."""

    def __init__(self, target: discord.Member):
        super().__init__(timeout=180)
        self.target = target

    @discord.ui.button(label="Warns", style=discord.ButtonStyle.secondary, emoji="⚠️")
    async def warns_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_warns(interaction, self.target)

    @discord.ui.button(label="Mod Logs", style=discord.ButtonStyle.secondary, emoji="📋")
    async def modlogs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_modlogs(interaction, limit=10)


class ModLogsView(discord.ui.View):
    """Attached to /modlogs — Prev/Next page through the log at a fixed
    page size, plus Refresh to reload the current page in place."""

    def __init__(self, page_size: int, page: int = 0, has_next: bool = False):
        super().__init__(timeout=180)
        self.page_size = page_size
        self.page = page
        self.prev_btn.disabled = page <= 0
        self.next_btn.disabled = not has_next

    @discord.ui.button(label="◀️ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_modlogs(interaction, limit=self.page_size, page=max(0, self.page - 1), edit=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_modlogs(interaction, limit=self.page_size, page=self.page, edit=True)

    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = get_mod_cog(interaction)
        await cog.send_modlogs(interaction, limit=self.page_size, page=self.page + 1, edit=True)
