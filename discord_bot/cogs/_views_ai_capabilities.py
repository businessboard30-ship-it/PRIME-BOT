"""The "what can you do?" panel the AI sends: a Components V2 card with a category picker.

WHO SEES WHAT: the specs passed in are already filtered to what the asker can actually run (permissions + the
command's own checks, see ai_command_guard.get_qualifying_commands), so a command someone cannot use is never
listed to them. The panel is also locked to the person who asked: anyone else who touches the picker is told to ask
for their own, because their permissions (and so their list) may differ.
"""
import discord

from modules import ai_capabilities as caps

_OVERVIEW = "overview"


class _CategorySelect(discord.ui.Select):
    def __init__(self, view: "CapabilitiesView", current: str):
        options = [discord.SelectOption(label="Overview", value=_OVERVIEW, emoji="🏠", default=current == _OVERVIEW)]
        for key in view.pages:
            emoji, label = caps.label_for(key)
            options.append(discord.SelectOption(label=label, value=key, emoji=emoji, default=current == key))
        super().__init__(placeholder="Pick a category", options=options, min_values=1, max_values=1)
        self._panel = view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=self._panel.render(self.values[0]))


class CapabilitiesView(discord.ui.LayoutView):
    def __init__(self, specs, asker_id: int, in_server: bool = True, current: str = _OVERVIEW):
        super().__init__(timeout=600)
        self.asker_id = asker_id
        self.in_server = in_server
        self.specs = list(specs)
        self.pages = caps.build_pages(self.specs) if in_server else {}
        self.render(current)

    def render(self, current: str) -> "CapabilitiesView":
        self.clear_items()
        if current != _OVERVIEW and current not in self.pages:
            current = _OVERVIEW
        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        if current == _OVERVIEW:
            container.add_item(discord.ui.TextDisplay("## 🤖 What I can do for you"))
            container.add_item(discord.ui.TextDisplay(caps.overview_text(self.specs, self.in_server)))
        else:
            emoji, label = caps.label_for(current)
            container.add_item(discord.ui.TextDisplay(f"## {emoji} {label}"))
            container.add_item(discord.ui.TextDisplay(self.pages[current]))
            container.add_item(discord.ui.TextDisplay("-# 🤖 = AI-only, no slash command. Just ask me in plain words."))
        if self.pages:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(_CategorySelect(self, current)))
        self.add_item(container)
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.asker_id:
            return True
        await interaction.response.send_message(
            "This list is built from the permissions of the person who asked. Ask me **what can you do** "
            "yourself to see yours.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        self.stop()
