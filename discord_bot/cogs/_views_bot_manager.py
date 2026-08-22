"""
Select-menu-driven management panel for /botmanager, so a user can pick a
registered bot from a dropdown (name shown, id resolved under the hood)
instead of first running /botmanager list to look up an id and retyping
it into every other subcommand. The bot_id-based commands themselves are
untouched — this is an additive UI on top of the same functions.
"""

import discord

from modules.discord_bot_manager import (
    get_managed_bot, remove_managed_bot,
    set_bot_username, set_bot_description,
)


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-4:]}" if len(token) > 14 else "•" * len(token)


def render_bot_panel(b: dict) -> str:
    return (
        f"**#{b['id']} — {b['name']}**\n"
        f"Username: {b.get('username') or '—'}\n"
        f"Token: `{_mask(b['token'])}`"
    )


class RenameModal(discord.ui.Modal, title="Rename bot"):
    def __init__(self, parent_view: "BotManagePanelView"):
        super().__init__()
        self.parent_view = parent_view
        self.username = discord.ui.TextInput(label="New username (2-32 chars)", max_length=32, required=True)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        b = self.parent_view.current_bot
        await interaction.response.defer(ephemeral=True)
        result = await set_bot_username(b["token"], str(self.username.value).strip())
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Discord rejected that change.')}", ephemeral=True)
            return
        b["username"] = str(self.username.value).strip()
        await interaction.edit_original_response(content=render_bot_panel(b), view=self.parent_view)
        await interaction.followup.send(f"✅ Renamed to **{self.username.value}**.", ephemeral=True)


class DescriptionModal(discord.ui.Modal, title="Update description"):
    def __init__(self, parent_view: "BotManagePanelView"):
        super().__init__()
        self.parent_view = parent_view
        self.description = discord.ui.TextInput(
            label="New description", style=discord.TextStyle.paragraph, max_length=400, required=True
        )
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        b = self.parent_view.current_bot
        await interaction.response.defer(ephemeral=True)
        result = await set_bot_description(b["token"], str(self.description.value).strip())
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Discord rejected that change.')}", ephemeral=True)
            return
        await interaction.followup.send("✅ Description updated.", ephemeral=True)


class BotPickerSelect(discord.ui.Select):
    def __init__(self, parent_view: "BotManagePanelView", bots: list):
        options = [
            discord.SelectOption(label=f"#{b['id']} {b['name']}"[:100], value=str(b["id"]))
            for b in bots
        ][:25]
        super().__init__(placeholder="Pick a bot to manage", options=options, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        bot_id = int(self.values[0])
        b = await get_managed_bot(interaction.user.id, bot_id)
        if not b:
            await interaction.response.send_message("That bot isn't registered to you anymore.", ephemeral=True)
            return
        self.parent_view.current_bot = b
        self.parent_view._sync_buttons()
        await interaction.response.edit_message(content=render_bot_panel(b), view=self.parent_view)


class BotManagePanelView(discord.ui.View):
    def __init__(self, bots: list, invoker_id: int):
        super().__init__(timeout=300)
        self.bots = bots
        self.invoker_id = invoker_id
        self.current_bot = None
        self.confirming_remove = False
        self.add_item(BotPickerSelect(self, bots))
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def _sync_buttons(self):
        has_bot = self.current_bot is not None
        self.rename_btn.disabled = not has_bot
        self.description_btn.disabled = not has_bot
        self.remove_btn.disabled = not has_bot
        self.remove_btn.label = "⚠️ Confirm remove?" if self.confirming_remove else "🗑️ Remove"
        self.remove_btn.style = discord.ButtonStyle.danger

    @discord.ui.button(label="✏️ Rename", style=discord.ButtonStyle.secondary, row=1)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RenameModal(self))

    @discord.ui.button(label="📝 Description", style=discord.ButtonStyle.secondary, row=1)
    async def description_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DescriptionModal(self))

    @discord.ui.button(label="🗑️ Remove", style=discord.ButtonStyle.danger, row=1)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.confirming_remove:
            self.confirming_remove = True
            self._sync_buttons()
            await interaction.response.edit_message(view=self)
            return
        ok = await remove_managed_bot(interaction.user.id, self.current_bot["id"])
        removed_name = self.current_bot["name"]
        self.current_bot = None
        self.confirming_remove = False
        self._sync_buttons()
        content = f"✅ Removed **{removed_name}**." if ok else "❌ Couldn't remove that bot."
        await interaction.response.edit_message(content=content, view=self)
