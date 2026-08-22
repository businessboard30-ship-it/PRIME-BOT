"""
Live toggle panel for /automod panel — one message with a button per
filter that flips it on/off in place and re-renders its own status,
instead of a separate `/automod toggle <filter> <bool>` invocation per
change. Sits alongside (doesn't replace) the existing toggle/status
commands, same additive approach as _views_bot_manager.py.

Components V2: unlike a plain discord.ui.View, a button's .style can't
just be flipped in place and left inside a LayoutView's Container — the
Container's ActionRow needs rebuilding so the edited message reflects
the new style, same rebuild-in-place approach as welcome.py's
WelcomeSetupView.
"""

import discord

from database import db

FILTER_FIELDS = {
    "word_filter": ("word_filter_enabled", "🚫 Word Filter"),
    "anti_invite": ("anti_invite_enabled", "🔗 Invite Filter"),
    "anti_mention": ("anti_mention_enabled", "📣 Mass Mention Filter"),
    "spam": ("spam_enabled", "⚡ Spam Filter"),
}


def render_panel_lines(s: dict) -> list:
    lines = []
    for key, (field, label) in FILTER_FIELDS.items():
        state = "✅ On" if s.get(field) else "⬜ Off"
        lines.append(f"{label} — {state}")
    lines.append(f"-# Action on trigger: {s.get('action') or 'delete'}")
    return lines


class AutomodPanelView(discord.ui.LayoutView):
    """The live toggle panel itself, as a Components V2 Container. Each
    filter is its own button in an ActionRow (max 4 fit on one row, same
    as FILTER_FIELDS' 4 entries); toggling one rebuilds the container's
    children so the new on/off styling shows immediately."""

    def __init__(self, config: dict, invoker_id: int, clone_id=None):
        super().__init__(timeout=300)
        self.config = config
        self.invoker_id = invoker_id
        self.clone_id = clone_id
        self.container = discord.ui.Container(accent_colour=discord.Color.blurple())
        self.add_item(self.container)
        self._build()

    def _build(self):
        self.container.clear_items()
        text = discord.ui.TextDisplay("\n".join(["### Auto-mod filters — tap to toggle", *render_panel_lines(self.config)]))
        row = discord.ui.ActionRow()
        for key, (field, label) in FILTER_FIELDS.items():
            enabled = bool(self.config.get(field))
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary)
            btn.callback = self._make_toggle_callback(field)
            row.add_item(btn)
        self.container.add_item(text)
        self.container.add_item(discord.ui.Separator())
        self.container.add_item(row)

    def _make_toggle_callback(self, field: str):
        async def _toggle(interaction: discord.Interaction):
            await interaction.response.defer()
            new_state = not self.config.get(field)
            await db.set_automod_config(interaction.guild_id, clone_id=self.clone_id, **{field: new_state})
            self.config[field] = new_state
            self._build()
            await interaction.edit_original_response(view=self)
        return _toggle

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.container.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
