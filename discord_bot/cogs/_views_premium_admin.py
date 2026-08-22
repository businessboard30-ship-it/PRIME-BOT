"""
Admin panel for premium groups — replaces typing a `group_id` copied from
/listpremium into editpremium/togglepremium with a Select Menu populated
from the guild's actual groups. Existing /editpremium, /togglepremium,
/verify commands are untouched; this is an additive UI on top of the same
db functions.
"""

import discord

from database import db


def render_group_line(g: dict, guild: discord.Guild) -> str:
    role = guild.get_role(g["role_id"]) if guild else None
    role_label = role.mention if role else f"(role {g['role_id']} not found)"
    status = "🟢 active" if g["active"] else "🔴 disabled"
    return f"**{g['name']}** — GHS {float(g['fee_ghs']):g} — {role_label} — {status}"


class EditPriceNameModal(discord.ui.Modal, title="Edit premium group"):
    def __init__(self, parent_view: "PremiumAdminPanelView"):
        super().__init__()
        self.parent_view = parent_view
        g = parent_view.current_group
        self.name = discord.ui.TextInput(label="Name", default=g["name"], max_length=100, required=True)
        self.price = discord.ui.TextInput(label="Price (GHS)", default=f"{float(g['fee_ghs']):g}", max_length=20, required=True)
        self.add_item(self.name)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            price = float(str(self.price.value))
            if price <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Price must be a number greater than 0.", ephemeral=True)
            return
        group_id = self.parent_view.current_group["group_id"]
        await db.update_premium_group(group_id, name=str(self.name.value), fee_ghs=price)
        self.parent_view.current_group["name"] = str(self.name.value)
        self.parent_view.current_group["fee_ghs"] = price
        await interaction.edit_original_response(
            content=render_group_line(self.parent_view.current_group, interaction.guild), view=self.parent_view
        )


class GroupPickerSelect(discord.ui.Select):
    def __init__(self, parent_view: "PremiumAdminPanelView", groups: list):
        options = [
            discord.SelectOption(
                label=f"#{g['group_id']} {g['name']}"[:100],
                description=f"GHS {float(g['fee_ghs']):g} — {'active' if g['active'] else 'disabled'}",
                value=str(g["group_id"]),
            )
            for g in groups
        ][:25]
        super().__init__(placeholder="Pick a premium group to manage", options=options, row=0)
        self.parent_view = parent_view
        self.groups_by_id = {g["group_id"]: g for g in groups}

    async def callback(self, interaction: discord.Interaction):
        group_id = int(self.values[0])
        self.parent_view.current_group = self.groups_by_id[group_id]
        self.parent_view._sync_buttons()
        await interaction.response.edit_message(
            content=render_group_line(self.parent_view.current_group, interaction.guild), view=self.parent_view
        )


class PremiumAdminPanelView(discord.ui.View):
    def __init__(self, groups: list, invoker_id: int):
        super().__init__(timeout=300)
        self.groups = groups
        self.invoker_id = invoker_id
        self.current_group = None
        self.add_item(GroupPickerSelect(self, groups))
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
        has_group = self.current_group is not None
        self.edit_btn.disabled = not has_group
        self.toggle_btn.disabled = not has_group
        if has_group:
            self.toggle_btn.label = "Disable" if self.current_group["active"] else "Enable"
            self.toggle_btn.style = discord.ButtonStyle.danger if self.current_group["active"] else discord.ButtonStyle.success

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary, row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditPriceNameModal(self))

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, row=1)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        new_state = not self.current_group["active"]
        await db.update_premium_group(self.current_group["group_id"], active=new_state)
        self.current_group["active"] = new_state
        self._sync_buttons()
        await interaction.edit_original_response(content=render_group_line(self.current_group, interaction.guild), view=self)
