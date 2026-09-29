# path: discord_bot/cogs/_views_style_wizard.py

"""Channel-name / text font-style wizard. Reached three ways: the
"styles" entry in _views_join_dm.py's FEATURE_TOGGLES (combined owner
join DM), the standalone /style command (style.py), and re-openable
anytime since every component here is a discord.ui.DynamicItem — same
survive-a-restart approach as the rest of the join-DM buttons.

Two independent tools, both landing on the same wizard message:
  - Convert: type any text, instantly get it back in every supported
    unicode font (modules/text_styles.py — deterministic, no AI).
  - Ask AI: describe a vibe/theme, get 5 plain-text channel-name ideas
    back (Groq via modules/ai_features.ai_chat, same per-tier daily cap
    every other AI feature already uses) — opt-in per use, nothing AI-
    generated is ever produced without the user tapping this button
    themselves.
  - Apply to a channel: pick a channel, type a base name, then pick a
    font style from a dropdown — renames the channel to the styled
    result. Needs manage_channels; checked before the rename is attempted.
"""

import re
import logging

import discord
from discord.ext import commands

from modules.text_styles import STYLES, apply_style, channel_name, style_choices
from modules.ai_features import ai_chat, check_ai_usage_limit
from modules.superbot_adapter import get_user_tier

logger = logging.getLogger(__name__)

# custom_id shape: style_<action>:<guild_id>:<clone_id or "-">
# Each DynamicItem gets its OWN template with the action baked in. They used
# to share one generic pattern, so discord.py routed every tap to whichever
# class registered last, and _decode read the action word as the guild id
# (int("chan") -> ValueError -> "Welcome Bot didn't respond in time").
_ID_CONVERT_RE = re.compile(r"^style_convert:(\d+):(-|\d+)$")
_ID_AI_RE = re.compile(r"^style_ai:(\d+):(-|\d+)$")
_ID_CHAN_RE = re.compile(r"^style_chan:(\d+):(-|\d+)$")


def _encode(action: str, guild_id: int, clone_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    return f"style_{action}:{guild_id}:{clone_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    clone_id = None if clone_part == "-" else int(clone_part)
    return guild_id, clone_id


AI_PROMPT_CONTEXT = (
    "You are suggesting Discord channel names. The user will describe a theme, vibe, or "
    "purpose for a channel. Reply with EXACTLY 5 short channel-name ideas, one per line, "
    "each 1-3 words, lowercase, hyphen-separated where natural (Discord channel-name style, "
    "e.g. 'clip-of-the-week'), each prefixed with a single fitting emoji. No numbering, no "
    "extra commentary, no markdown — just the 5 lines."
)


class _ConvertTextModal(discord.ui.Modal, title="Convert text to fancy fonts"):
    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.text = discord.ui.TextInput(
            label="Text to convert", style=discord.TextStyle.short,
            max_length=80, required=True, placeholder="general",
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.text.value).strip()
        lines = [f"{emoji} **{label}** — {apply_style(value, key)}" for key, label, emoji in style_choices()]
        await interaction.response.send_message(
            f"**Fonts for \u201c{value}\u201d:**\n" + "\n".join(lines) +
            "\n\n-# Copy whichever line's styled text you want — works as a channel name, nickname, or anywhere else.",
            ephemeral=True,
        )


class _StyleAIModal(discord.ui.Modal, title="Ask AI for channel-name ideas"):
    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.theme = discord.ui.TextInput(
            label="Describe the channel", style=discord.TextStyle.short,
            max_length=150, required=True, placeholder="cozy gaming lounge, casual vibe",
        )
        self.add_item(self.theme)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        tier = await get_user_tier(user_id)
        allowed, warning = await check_ai_usage_limit(user_id, tier, "messages")
        if not allowed:
            await interaction.followup.send(f"\u274c {warning}", ephemeral=True)
            return

        reply = await ai_chat(
            user_id, str(self.theme.value), tier=tier,
            command_context=AI_PROMPT_CONTEXT, guild_id=self.guild_id, kind="style_wizard",
        )
        if not reply:
            await interaction.followup.send("\u274c AI is having trouble right now — try again shortly.", ephemeral=True)
            return

        ideas = [line.strip("-\u2022 ") for line in reply.splitlines() if line.strip()][:5]
        if not ideas:
            await interaction.followup.send("Couldn't parse a suggestion out of that — try rephrasing.", ephemeral=True)
            return

        preview_keys = ["bold", "small_caps", "sans"]
        blocks = []
        for idea in ideas:
            styled = " \u2022 ".join(apply_style(idea, k) for k in preview_keys)
            blocks.append(f"**{idea}**\n{styled}")
        await interaction.followup.send(
            "**Ideas for \u201c" + str(self.theme.value) + "\u201d:**\n\n" + "\n\n".join(blocks) +
            "\n\n-# Like one? Use \ud83d\udd24 Convert text above to see it in every font, "
            "or \ud83c\udfaf Apply to a channel to rename with it directly.",
            ephemeral=True,
        )


class _ChannelRenameModal(discord.ui.Modal, title="New channel name"):
    def __init__(self, guild_id: int, clone_id, channel_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.channel_id = channel_id
        self.name = discord.ui.TextInput(
            label="Base name (plain text)", style=discord.TextStyle.short,
            max_length=80, required=True, placeholder="general",
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        base_name = str(self.name.value).strip()
        view = _StyleApplySelectView(self.guild_id, self.clone_id, self.channel_id, base_name)
        await interaction.response.send_message(
            f"Pick a font to rename the channel to **{base_name}** styled:", view=view, ephemeral=True,
        )


class _StyleApplySelectView(discord.ui.View):
    """One-shot follow-up after the rename modal — doesn't need to
    survive a restart (it only exists for the few seconds between the
    modal submit and the user picking a style right after), unlike the
    DynamicItem components above that sit in a standing wizard message."""

    def __init__(self, guild_id: int, clone_id, channel_id: int, base_name: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.channel_id = channel_id
        self.base_name = base_name

        self.select = discord.ui.Select(
            placeholder="Choose a font style\u2026",
            # NO emoji= here: style_choices()'s "emoji" is a sample glyph
            # (e.g. U+1D401 bold B), not a real emoji, and Discord rejects
            # non-emoji characters in a SelectOption with a 400 "Invalid
            # emoji" — which killed the whole message and surfaced as "bot
            # didn't respond". The description carries a live preview of
            # the user's own text in each font instead.
            options=[
                discord.SelectOption(
                    label=label, value=key,
                    description=channel_name(base_name, key)[:100] or label,
                )
                for key, label, _sample in style_choices()
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        style_key = self.select.values[0]
        font_label = STYLES[style_key][0]

        guild = interaction.client.get_guild(self.guild_id)
        channel = guild.get_channel(self.channel_id) if guild else None
        if channel is None:
            await interaction.response.send_message("That channel isn't around anymore.", ephemeral=True)
            return
        perms = channel.permissions_for(guild.me)
        if not perms.manage_channels:
            await interaction.response.send_message(
                "I don't have **Manage Channels** permission there \u2014 grant it and try again.", ephemeral=True,
            )
            return

        # Discord channel names are lowercase (and text channels can't hold
        # spaces) — see text_styles.channel_name. Voice/stage keep spaces.
        styled_name = channel_name(
            self.base_name, style_key,
            hyphenate=not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)),
        )

        # Acknowledge instantly AND show which font was picked: the chosen
        # option stays highlighted in the dropdown and the message text
        # names it. (Discord rate-limits channel renames to 2 per 10
        # minutes, so the edit below can take a while — this keeps the
        # interaction alive and tells the user what's happening.)
        for opt in self.select.options:
            opt.default = (opt.value == style_key)
        await interaction.response.edit_message(
            content=f"\u23f3 Applying **{font_label}** \u2192 {styled_name}\u2026", view=self,
        )
        try:
            await channel.edit(name=styled_name[:100], reason=f"Styled via /style by {interaction.user}")
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"\u274c Discord rejected that name: {e.text}", view=None,
            )
            return
        await interaction.edit_original_response(
            content=f"\u2705 {channel.mention} renamed using **{font_label}**:\n{styled_name}", view=None,
        )
        self.stop()


class _StyleConvertButton(discord.ui.DynamicItem[discord.ui.Button], template=_ID_CONVERT_RE.pattern):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Convert text", style=discord.ButtonStyle.primary, emoji="\ud83d\udd24",
            custom_id=_encode("convert", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ConvertTextModal(self.guild_id, self.clone_id))


class _StyleAIButton(discord.ui.DynamicItem[discord.ui.Button], template=_ID_AI_RE.pattern):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Ask AI", style=discord.ButtonStyle.secondary, emoji="\u2728",
            custom_id=_encode("ai", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_StyleAIModal(self.guild_id, self.clone_id))


class _StyleChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_ID_CHAN_RE.pattern):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="\ud83c\udfaf Apply to a channel\u2026",
            channel_types=[discord.ChannelType.text, discord.ChannelType.voice, discord.ChannelType.forum],
            min_values=1, max_values=1,
            custom_id=_encode("chan", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        channel = self.item.values[0]
        await interaction.response.send_modal(_ChannelRenameModal(self.guild_id, self.clone_id, channel.id))


class StyleWizardLayoutView(discord.ui.LayoutView):
    """Components V2 entry point — Convert / Ask AI buttons plus the
    channel-target select, all DynamicItem so this survives however long
    it sits in a DM or channel before someone taps it."""

    def __init__(self, guild_id: int, clone_id=None):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(
            "## \ud83d\udd24 Channel Names & Fonts\n"
            "Style any text into a fancy unicode font, or describe a vibe and let AI suggest "
            "channel names \u2014 then apply the result straight to a channel."
        ))
        container.add_item(discord.ui.ActionRow(
            _StyleConvertButton(guild_id, clone_id),
            _StyleAIButton(guild_id, clone_id),
        ))
        container.add_item(discord.ui.ActionRow(_StyleChannelSelect(guild_id, clone_id)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# AI suggestions only run when you tap **Ask AI** \u2014 nothing is generated automatically."
        ))
        self.add_item(container)


STYLE_WIZARD_DYNAMIC_ITEMS = (
    _StyleConvertButton, _StyleAIButton, _StyleChannelSelect,
)


async def setup(bot: commands.Bot):
    # No cog here — this module only supplies the view/DynamicItems that
    # style.py's cog and _views_join_dm.py's "styles" feature entry import.
    pass
