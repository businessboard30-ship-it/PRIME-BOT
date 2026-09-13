"""Custom Role wizard (see custom_role.py's /customrole). Session-scoped
discord.ui.View (timeout, not DynamicItems) — same shape as
verification.py's WizardView, since this is a short-lived, one-invoker-at-a-
time flow, not something that needs to survive a bot restart.

Steps: Name (modal) -> Font style (20-option select) -> Color (hue-family
buttons -> 20-shade select, or a custom-hex modal) -> Icon (optional emoji
select) -> Confirm (creates/edits the role + slots it below the bot's top
role, reusing verification.py's edit_role_positions pattern).
"""

import logging
import re
import unicodedata

import discord

from database import db
from config import CUSTOM_ROLE_FONT_STYLES, CUSTOM_ROLE_COLOR_PALETTE

logger = logging.getLogger(__name__)

# Built once from the (label, example) pairs in config.py: maps each
# style's example transformation of "Sample" back onto plain
# "Sample" character-by-character, giving a plain-char -> styled-char table
# usable on arbitrary buyer input (not just the word "Sample").
_SOURCE_WORD = "Sample"


def _build_font_map(example: str) -> dict:
    mapping = {}
    for plain_char, styled_char in zip(_SOURCE_WORD, example):
        mapping.setdefault(plain_char.lower(), styled_char.lower() if plain_char.islower() else styled_char)
        mapping.setdefault(plain_char.upper(), styled_char.upper() if plain_char.isupper() else styled_char)
    return mapping


_FONT_MAPS = {key: _build_font_map(example) for key, (_, example) in CUSTOM_ROLE_FONT_STYLES.items()}

# A handful of ASCII-safe letters/digits not present in "Sample" — best-effort
# fallback for those using each style's own Unicode block, so typed names
# beyond a/S/m/p/l/e/digits still transform instead of falling back to plain.
_UNICODE_BLOCK_OFFSETS = {
    "bold": (0x1D400, 0x1D41A, 0x1D7CE),
    "italic": (0x1D434, 0x1D44E, None),
    "bold_italic": (0x1D468, 0x1D482, None),
    "sans": (0x1D5A0, 0x1D5BA, 0x1D7E2),
    "sans_bold": (0x1D5D4, 0x1D5EE, 0x1D7EC),
    "sans_italic": (0x1D608, 0x1D622, None),
    "sans_bold_italic": (0x1D63C, 0x1D656, None),
    "double_struck": (0x1D538, 0x1D552, 0x1D7D8),
    "monospace": (0x1D670, 0x1D68A, 0x1D7F6),
    "fraktur": (0x1D504, 0x1D51E, None),
    "bold_fraktur": (0x1D56C, 0x1D586, None),
    "script": (0x1D49C, 0x1D4B6, None),
    "bold_script": (0x1D4D0, 0x1D4EA, None),
}


def apply_font_style(text: str, style_key: str) -> str:
    """Best-effort per-character transform. Falls back to the original
    character for anything a style's block doesn't cover (spaces,
    punctuation, or letters outside a/z0-9 for styles with no clean
    contiguous Unicode block, e.g. circled/small_caps/fullwidth beyond
    what _build_font_map already captured from the "Sample" example)."""
    if style_key == "plain" or style_key not in CUSTOM_ROLE_FONT_STYLES:
        return text
    offsets = _UNICODE_BLOCK_OFFSETS.get(style_key)
    out = []
    for ch in text:
        if offsets and (ch.isalpha() or ch.isdigit()):
            upper_base, lower_base, digit_base = offsets
            try:
                if ch.isdigit() and digit_base:
                    out.append(chr(digit_base + int(ch)))
                    continue
                if ch.isupper() and upper_base:
                    out.append(chr(upper_base + (ord(ch) - ord("A"))))
                    continue
                if ch.islower() and lower_base:
                    out.append(chr(lower_base + (ord(ch) - ord("a"))))
                    continue
            except ValueError:
                pass
        out.append(_FONT_MAPS.get(style_key, {}).get(ch, ch))
    return "".join(out)


_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class CustomRoleWizardView(discord.ui.View):
    def __init__(self, invoker_id: int, guild_id: int, clone_id, existing: dict | None):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.guild_id = guild_id
        self.clone_id = clone_id
        existing = existing or {}
        self.base_name: str = existing.get("base_name") or ""
        self.font_style: str = existing.get("font_style") or "plain"
        self.color_hex: str = existing.get("color_hex") or "#5865F2"
        self.icon: str | None = existing.get("icon")
        self.existing_role_id = existing.get("role_id")
        self.selected_family: str | None = None

        self.add_item(_NameButton(self))
        self.add_item(_FontSelect(self))
        for family_name in CUSTOM_ROLE_COLOR_PALETTE:
            self.add_item(_FamilyButton(self, family_name))
        self.add_item(_CustomHexButton(self))
        self.add_item(_IconSelect(self))
        self.add_item(_ConfirmButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This wizard isn't yours — run /customrole yourself.", ephemeral=True)
            return False
        return True

    def styled_preview(self) -> str:
        name = self.base_name or "YourName"
        return apply_font_style(name, self.font_style)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎨 Custom Role Wizard", color=discord.Color(int(self.color_hex.lstrip("#"), 16)))
        embed.add_field(name="Name", value=self.base_name or "*(not set — tap Set Name)*", inline=False)
        style_label = CUSTOM_ROLE_FONT_STYLES.get(self.font_style, ("Plain", None))[0] if self.font_style != "plain" else "Plain"
        embed.add_field(name="Font style", value=style_label, inline=True)
        embed.add_field(name="Color", value=self.color_hex, inline=True)
        embed.add_field(name="Icon", value=self.icon or "*(none)*", inline=True)
        embed.add_field(name="Preview", value=f"### {self.styled_preview()}", inline=False)
        embed.set_footer(text="This role updates only when you tap Create/Update Role.")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, _ShadeSelect):
                self.remove_item(child)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)


class _NameModal(discord.ui.Modal, title="Custom role name"):
    def __init__(self, wizard: CustomRoleWizardView):
        super().__init__()
        self.wizard = wizard
        self.name_input = discord.ui.TextInput(
            label="Role name (before styling)",
            default=wizard.base_name,
            max_length=32,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.name_input.value).strip()
        if name.lower() in ("@everyone", "@here", "everyone", "here"):
            await interaction.response.send_message("That name isn't allowed.", ephemeral=True)
            return
        guild = interaction.guild
        existing_role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if existing_role and existing_role.id != self.wizard.existing_role_id:
            await interaction.response.send_message(
                f"A role called **{existing_role.name}** already exists — pick a different name.",
                ephemeral=True,
            )
            return
        self.wizard.base_name = name
        await self.wizard.refresh(interaction)


class _NameButton(discord.ui.Button):
    def __init__(self, wizard: CustomRoleWizardView):
        self.wizard = wizard
        super().__init__(label="1️⃣ Set Name", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_NameModal(self.wizard))


class _FontSelect(discord.ui.Select):
    def __init__(self, wizard: CustomRoleWizardView):
        self.wizard = wizard
        options = [discord.SelectOption(label="Plain (no styling)", value="plain")]
        for key, (label, example) in CUSTOM_ROLE_FONT_STYLES.items():
            options.append(discord.SelectOption(label=label, value=key, description=example[:100]))
        super().__init__(placeholder="2️⃣ Font style", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.font_style = self.values[0]
        await self.wizard.refresh(interaction)


class _FamilyButton(discord.ui.Button):
    def __init__(self, wizard: CustomRoleWizardView, family_name: str):
        self.wizard = wizard
        self.family_name = family_name
        super().__init__(label=f"3️⃣ {family_name}", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.selected_family = self.family_name
        for child in list(self.wizard.children):
            if isinstance(child, _ShadeSelect):
                self.wizard.remove_item(child)
        self.wizard.add_item(_ShadeSelect(self.wizard, self.family_name))
        await self.wizard.refresh(interaction)


class _ShadeSelect(discord.ui.Select):
    def __init__(self, wizard: CustomRoleWizardView, family_name: str):
        self.wizard = wizard
        shades = CUSTOM_ROLE_COLOR_PALETTE[family_name]
        options = [
            discord.SelectOption(label=f"Shade {i + 1}", value=hexcode, description=hexcode)
            for i, hexcode in enumerate(shades)
        ]
        super().__init__(placeholder=f"Pick a shade of {family_name}", options=options, row=3)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.color_hex = self.values[0]
        await self.wizard.refresh(interaction)


class _CustomHexModal(discord.ui.Modal, title="Custom hex color"):
    def __init__(self, wizard: CustomRoleWizardView):
        super().__init__()
        self.wizard = wizard
        self.hex_input = discord.ui.TextInput(label="Hex code (e.g. #FF8800)", max_length=7, required=True)
        self.add_item(self.hex_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.hex_input.value).strip()
        if not _HEX_RE.match(value):
            await interaction.response.send_message("That's not a valid 6-digit hex code, e.g. `#FF8800`.", ephemeral=True)
            return
        self.wizard.color_hex = value if value.startswith("#") else f"#{value}"
        await self.wizard.refresh(interaction)


class _CustomHexButton(discord.ui.Button):
    def __init__(self, wizard: CustomRoleWizardView):
        self.wizard = wizard
        super().__init__(label="Custom hex…", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_CustomHexModal(self.wizard))


_ICON_EMOJIS = ["⭐", "🔥", "💎", "👑", "🌙", "⚡", "🎯", "🦋", "🍀", "🎮",
                "🎵", "🌸", "☠️", "🐺", "🦁", "🕊️", "🧊", "🌈", "⚔️", "🔮"]


class _IconSelect(discord.ui.Select):
    def __init__(self, wizard: CustomRoleWizardView):
        self.wizard = wizard
        options = [discord.SelectOption(label="No icon", value="__none__")]
        options += [discord.SelectOption(label=emoji, value=emoji, emoji=emoji) for emoji in _ICON_EMOJIS]
        super().__init__(placeholder="4️⃣ Icon (optional)", options=options, row=4)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        self.wizard.icon = None if value == "__none__" else value
        await self.wizard.refresh(interaction)


class _ConfirmButton(discord.ui.Button):
    def __init__(self, wizard: CustomRoleWizardView):
        self.wizard = wizard
        super().__init__(label="✅ Create/Update Role", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if not wizard.base_name:
            await interaction.response.send_message("Set a name first (Step 1).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        bot_member = guild.me
        styled_name = wizard.styled_preview()[:100]
        color = discord.Color(int(wizard.color_hex.lstrip("#"), 16))

        role = guild.get_role(wizard.existing_role_id) if wizard.existing_role_id else None

        # Boost Level 2 gates ROLE_ICONS — best-effort, skip rather than fail.
        icon_kwargs = {}
        if wizard.icon and "ROLE_ICONS" in guild.features:
            icon_kwargs["display_icon"] = wizard.icon

        try:
            if role is None:
                role = await guild.create_role(
                    name=styled_name, color=color, reason=f"Custom role purchased by {interaction.user}",
                    **icon_kwargs,
                )
            else:
                await role.edit(name=styled_name, color=color, reason="Custom role restyled", **icon_kwargs)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create/edit roles here — ask an admin to grant me **Manage Roles**.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Couldn't save the role: {e}", ephemeral=True)
            return

        if role.id != wizard.existing_role_id:
            if bot_member.top_role.position <= 1:
                await interaction.followup.send(
                    f"✅ Created {role.mention}, but my own role is at (or near) the bottom of the role "
                    "list, so I can't slot yours below me or assign it. Ask an admin to move my role up, "
                    "then run /customrole again.",
                    ephemeral=True,
                )
                return
            target_position = bot_member.top_role.position - 1
            try:
                await guild.edit_role_positions(positions={role: target_position})
            except discord.HTTPException:
                logger.warning("custom_role: failed to reposition role %s in guild %s", role.id, guild.id)
            try:
                await interaction.user.add_roles(role, reason="Custom role purchased")
            except discord.HTTPException:
                logger.warning("custom_role: failed to assign role %s to user %s", role.id, interaction.user.id)

        await db.save_custom_role(
            guild.id, interaction.user.id, role.id, wizard.base_name, wizard.font_style,
            wizard.color_hex, wizard.icon, clone_id=wizard.clone_id,
        )
        for child in wizard.children:
            child.disabled = True
        await interaction.edit_original_response(content=f"🎉 Done — {role.mention} is all set!", embed=wizard.build_embed(), view=wizard)
        wizard.stop()
