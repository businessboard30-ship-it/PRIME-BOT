# path: discord_bot/cogs/style.py

"""/style — the ONLY command in this cog. Type text once (a normal
slash-command argument, not a popup), then everything else — convert to
fonts, ask AI for name ideas, apply the styled result to a channel — is
buttons/a select on that one result view. No discord.ui.Modal anywhere
in this file, and no second/third slash command: renaming used to be its
own /stylerename command, but that's now just the "Apply to a channel"
select on this same result view (_StyleApplyChannelSelect ->
_StyleApplySelectView, both below) — one command, one wizard, everything
lives there.

The channel-name/font wizard reachable from the combined owner join DM's
"styles" feature button (_views_join_dm.py) is a SEPARATE, unrelated
entry point — it still lives in _views_style_wizard.py and still uses
modals there, since it has no slash-command argument to source text
from and isn't in scope here; nothing in this file touches it.

Because /stylerename no longer exists as an independently-runnable slash
command, it's also gone from modules/ai_command_allowlist.py's
AI_COMMANDS — /aichat can no longer natural-language-trigger a channel
rename directly. That's an intentional tradeoff of "only one command":
the allowlist mechanism calls a real slash command's callback, and there
isn't a standalone rename command left to call. If AI-triggered renames
are wanted back later, that needs its own command again (or a tool-call
path into this wizard), not a decision to make silently here."""

import logging
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._views_style_wizard import _StyleApplySelectView
from modules.text_styles import STYLES, apply_style, style_choices
from modules.ai_features import ai_chat, check_ai_usage_limit
from modules.superbot_adapter import get_user_tier

logger = logging.getLogger(__name__)

AI_PROMPT_CONTEXT = (
    "You are suggesting Discord channel names. The user will describe a theme, vibe, or "
    "purpose for a channel. Reply with EXACTLY 5 short channel-name ideas, one per line, "
    "each 1-3 words, lowercase, hyphen-separated where natural (Discord channel-name style, "
    "e.g. 'clip-of-the-week'), each prefixed with a single fitting emoji. No numbering, no "
    "extra commentary, no markdown — just the 5 lines."
)

_FONT_CHOICES = [app_commands.Choice(name=label, value=key) for key, label, _sample in style_choices()]
_RENAMABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)
_MAX_TEXT = 60


def _normalize_font(value) -> Optional[str]:
    """Accepts a style key ("bold_italic"), a label ("Bold Italic"), or a
    close variant ("bold-italic") — the AI doesn't always echo the enum."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    key = raw.replace(" ", "_").replace("-", "_")
    if key in STYLES:
        return key
    for k, (label, _s, _m) in STYLES.items():
        if label.lower() == raw:
            return k
    return None


async def _apply_font_to_channel(interaction: discord.Interaction, text: str, style_key: str, channel) -> None:
    """Rename `channel` to `text` in font `style_key`. Shared by the slash
    command and the AI handler. Assumes interaction.response is already
    used (deferred, or consumed by the AI Confirm click) — always replies
    via followup. Re-checks both the user's and the bot's Manage Channels
    on THAT channel, since this is the one place a rename actually happens."""
    if not isinstance(channel, _RENAMABLE):
        await interaction.followup.send("\u274c I can only rename text, voice, stage and forum channels.", ephemeral=True)
        return
    if not channel.permissions_for(interaction.user).manage_channels:
        await interaction.followup.send(f"\u274c You need **Manage Channels** on {channel.mention} to rename it.", ephemeral=True)
        return
    if not channel.permissions_for(interaction.guild.me).manage_channels:
        await interaction.followup.send(
            f"\u274c I don't have **Manage Channels** on {channel.mention} \u2014 grant it and try again.", ephemeral=True,
        )
        return
    styled = apply_style(text, style_key)[:100]
    if not styled.strip():
        await interaction.followup.send("\u274c Nothing to rename it to \u2014 give me some text.", ephemeral=True)
        return
    try:
        await channel.edit(name=styled, reason=f"Styled via /style by {interaction.user}")
    except discord.HTTPException as e:
        await interaction.followup.send(f"\u274c Discord rejected that name: {e.text}", ephemeral=True)
        return
    await interaction.followup.send(
        f"\u2705 {channel.mention} renamed using **{STYLES[style_key][0]}**:\n{styled}", ephemeral=True,
    )


class _StyleAIButton(discord.ui.Button):
    """Runs the AI vibe->channel-name-ideas flow straight off the text the
    user already typed into /style — no modal asking them to re-type it.
    Not a DynamicItem: this only needs to live for the few minutes the
    ephemeral result message is on screen, not survive a restart."""
    def __init__(self, text: str, guild_id: int, clone_id):
        super().__init__(label="Ask AI for name ideas", style=discord.ButtonStyle.secondary, emoji="\u2728")
        self.text = text
        self.guild_id = guild_id
        self.clone_id = clone_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        tier = await get_user_tier(user_id)
        allowed, warning = await check_ai_usage_limit(user_id, tier, "messages")
        if not allowed:
            await interaction.followup.send(f"\u274c {warning}", ephemeral=True)
            return

        reply = await ai_chat(
            user_id, self.text, tier=tier,
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
            f"**Ideas for \u201c{self.text}\u201d:**\n\n" + "\n\n".join(blocks) +
            "\n\n-# Like one? Run /style again with that text, then use **Apply to a channel** below.",
            ephemeral=True,
        )


class _StyleApplyChannelSelect(discord.ui.ChannelSelect):
    """The entire former /stylerename command, folded into this one
    select: pick a channel here, it goes straight to the font Select
    below (_StyleApplySelectView, already modal-free) using the text
    from /style as the base name. No separate command, no "type the
    base name" modal — this IS /stylerename now."""
    def __init__(self, text: str, guild_id: int, clone_id):
        super().__init__(
            placeholder="\ud83c\udfaf Apply to a channel\u2026",
            channel_types=[discord.ChannelType.text, discord.ChannelType.voice, discord.ChannelType.forum],
            min_values=1, max_values=1,
        )
        self.text = text
        self.guild_id = guild_id
        self.clone_id = clone_id

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "You need **Manage Channels** to rename a channel.", ephemeral=True,
            )
            return
        channel = self.values[0]
        view = _StyleApplySelectView(self.guild_id, self.clone_id, channel.id, self.text)
        await interaction.response.send_message(
            f"Pick a font to rename {channel.mention} to **{self.text}** styled:", view=view, ephemeral=True,
        )


class _StyleResultView(discord.ui.LayoutView):
    """The whole /style result — every field it needs (the text) was
    already typed as the command's own argument, so nothing here ever
    needs to pop up a text box, and there's no second command to run
    for any of it: fonts, AI ideas, and the channel rename are all right
    here."""
    def __init__(self, text: str, guild_id: int, clone_id):
        super().__init__(timeout=300)
        lines = [f"{emoji} **{label}** — {apply_style(text, key)}" for key, label, emoji in style_choices()]
        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(
            f"## \ud83d\udd24 \u201c{text}\u201d in every font\n" + "\n".join(lines)
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_StyleAIButton(text, guild_id, clone_id)))
        container.add_item(discord.ui.ActionRow(_StyleApplyChannelSelect(text, guild_id, clone_id)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# Asking AI only runs when you tap the button \u2014 nothing is generated automatically."
        ))
        self.add_item(container)


class StyleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # AI tool-calling (/aichat + reply chat): renaming a channel mutates the
    # server, so ai_command_allowlist marks /style requires_confirmation for
    # any call that passes font/channel (a bare preview needs none). The
    # AIConfirmView click has already consumed the interaction's response,
    # so the confirmed call goes to ai_style (followup-only) instead of the
    # slash callback, and ai_style_prompt writes the confirm text + refuses
    # early (no confirm button) when it could never succeed.
    AI_CONFIRMED_HANDLERS = {"style": "ai_style"}
    AI_CONFIRM_PROMPTS = {"style": "ai_style_prompt"}

    def _ai_check(self, interaction, text, font, channel):
        """(error_or_None, clean_text, style_key_or_None) for an AI call."""
        text = (text or "").strip()[:_MAX_TEXT]
        if not text:
            return "\u274c What text should I style?", text, None
        style_key = None
        if font not in (None, ""):
            style_key = _normalize_font(font)
            if style_key is None:
                return f"\u274c I don't know a font called \u201c{font}\u201d.", text, None
        if channel is None:
            return ("\u274c Which channel? Mention it (#channel), give its name, or say \u201cthis channel\u201d."), text, style_key
        if not isinstance(channel, _RENAMABLE):
            return "\u274c I can only rename text, voice, stage and forum channels.", text, style_key
        if not channel.permissions_for(interaction.user).manage_channels:
            return f"\u274c You need **Manage Channels** on {channel.mention} to rename it.", text, style_key
        if not channel.permissions_for(interaction.guild.me).manage_channels:
            return f"\u274c I don't have **Manage Channels** on {channel.mention}.", text, style_key
        return None, text, style_key

    async def ai_style_prompt(self, interaction, text=None, font=None, channel=None, **_ignored):
        err, text, style_key = self._ai_check(interaction, text, font, channel)
        if err:
            return False, err
        if style_key is None:
            return True, f"Open the font picker to rename {channel.mention} using **{text}**?"
        styled = apply_style(text, style_key)[:100]
        return True, f"Rename {channel.mention} to **{styled}** ({STYLES[style_key][0]})?"

    async def ai_style(self, interaction, text=None, font=None, channel=None, **_ignored):
        err, text, style_key = self._ai_check(interaction, text, font, channel)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        if style_key is None:
            clone_id = getattr(self.bot, "clone_id", None)
            view = _StyleApplySelectView(interaction.guild.id, clone_id, channel.id, text)
            await interaction.followup.send(
                f"Pick a font to rename {channel.mention} to **{text}** styled:", view=view, ephemeral=True,
            )
            return
        await _apply_font_to_channel(interaction, text, style_key, channel)

    @app_commands.command(
        name="style",
        description="Style text into fancy fonts, get AI channel-name ideas from it, or apply it to a channel",
    )
    @app_commands.describe(
        text="The text to style (a channel base name, or a vibe/theme for AI ideas)",
        font="Rename a channel with this font right away (needs channel). Leave empty to preview every font",
        channel="Channel to rename (leave font empty to pick the font from a menu)",
    )
    @app_commands.choices(font=_FONT_CHOICES)
    async def style(
        self, interaction: discord.Interaction, text: str,
        font: Optional[app_commands.Choice[str]] = None,
        channel: Optional[Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]] = None,
    ):
        guild_id = interaction.guild.id if interaction.guild else None
        clone_id = getattr(self.bot, "clone_id", None)
        if guild_id is None:
            await interaction.response.send_message(
                "Run this in a server \u2014 applying to a channel needs one, and AI ideas need a "
                "server to reply in.", ephemeral=True,
            )
            return
        text = text[:_MAX_TEXT].strip()
        if not text:
            await interaction.response.send_message("Give me some text to style.", ephemeral=True)
            return

        if font is None and channel is None:
            await interaction.response.send_message(
                view=_StyleResultView(text, guild_id, clone_id), ephemeral=True,
            )
        elif font is None:
            # channel only -> font picker for that channel
            if not channel.permissions_for(interaction.user).manage_channels:
                await interaction.response.send_message(
                    f"You need **Manage Channels** on {channel.mention} to rename it.", ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Pick a font to rename {channel.mention} to **{text}** styled:",
                view=_StyleApplySelectView(guild_id, clone_id, channel.id, text), ephemeral=True,
            )
        elif channel is None:
            await interaction.response.send_message(
                "Pick which channel to rename with the `channel` option.", ephemeral=True,
            )
        else:
            await interaction.response.defer(ephemeral=True)
            await _apply_font_to_channel(interaction, text, font.value, channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(StyleCog(bot))
