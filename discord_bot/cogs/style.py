# path: discord_bot/cogs/style.py

"""/style — one command, one line of typed text (a normal slash-command
argument, not a popup), then everything after that (convert to fonts, ask
AI for name ideas, apply to a channel) is buttons/selects on the result
view — no discord.ui.Modal anywhere in this file. The channel-name/font
wizard reachable from the combined owner join DM's "styles" feature
button (_views_join_dm.py) is a SEPARATE, unrelated entry point — it
still lives in _views_style_wizard.py and still uses modals there, since
it has no slash-command argument to source text from and isn't in scope
here; changing this command's shape doesn't touch it.

/stylerename is a real, independently-runnable slash command (not just a
button on the result view) for exactly one reason: being in
modules/ai_command_allowlist.py's AI_COMMANDS is what lets /aichat (and
reply/mention chat) call it as a natural-language tool call — "rename
#chat to Gaming Lounge in small caps, implement it" runs through here
via the existing AI-command tool-calling pipeline in
ai_command_guard.py/ai_tools.py. It mutates a real channel, so it's
requires_confirmation=True in the allowlist — the AI must get an
explicit Confirm click (AIConfirmView) before this callback ever runs,
same as kick/ban/purge.

REMOVED: /stylepreview. It only ever existed as a read-only twin of this
command's Convert step for the AI-tool-calling allowlist (see the
allowlist's comment for /stylerename above) — now that /style's own
Convert button works straight off a typed argument with no modal in the
way, that duplicate command added nothing a person couldn't already get
from /style itself, so it's gone along with its allowlist entry."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._views_style_wizard import _StyleApplySelectView
from modules.text_styles import apply_style, style_choices
from modules.ai_features import ai_chat, check_ai_usage_limit
from modules.superbot_adapter import get_user_tier

logger = logging.getLogger(__name__)

_STYLE_APP_CHOICES = [app_commands.Choice(name=label, value=key) for key, label, _emoji in style_choices()]

AI_PROMPT_CONTEXT = (
    "You are suggesting Discord channel names. The user will describe a theme, vibe, or "
    "purpose for a channel. Reply with EXACTLY 5 short channel-name ideas, one per line, "
    "each 1-3 words, lowercase, hyphen-separated where natural (Discord channel-name style, "
    "e.g. 'clip-of-the-week'), each prefixed with a single fitting emoji. No numbering, no "
    "extra commentary, no markdown — just the 5 lines."
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
            "\n\n-# Like one? Run `/stylerename` to apply it to a channel directly.",
            ephemeral=True,
        )


class _StyleApplyChannelSelect(discord.ui.ChannelSelect):
    """Picking a channel here goes straight to the font Select
    (_StyleApplySelectView, already modal-free) using the text from
    /style as the base name — no "type the base name" modal in between."""
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
        channel = self.values[0]
        view = _StyleApplySelectView(self.guild_id, self.clone_id, channel.id, self.text)
        await interaction.response.send_message(
            f"Pick a font to rename {channel.mention} to **{self.text}** styled:", view=view, ephemeral=True,
        )


class _StyleResultView(discord.ui.LayoutView):
    """The whole /style result — every field it needs (the text) was
    already typed as the command's own argument, so nothing here ever
    needs to pop up a text box of its own."""
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

    @app_commands.command(
        name="style",
        description="Style text into fancy fonts, get AI channel-name ideas from it, or apply it to a channel",
    )
    @app_commands.describe(text="The text to style (a channel base name, or a vibe/theme for AI ideas)")
    async def style(self, interaction: discord.Interaction, text: str):
        guild_id = interaction.guild.id if interaction.guild else None
        clone_id = getattr(self.bot, "clone_id", None)
        if guild_id is None:
            await interaction.response.send_message(
                "Run this in a server \u2014 applying to a channel needs one, and AI ideas need a "
                "server to reply in.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=_StyleResultView(text[:80].strip(), guild_id, clone_id), ephemeral=True,
        )

    @app_commands.command(name="stylerename", description="Style text into a font and rename a channel to it")
    @app_commands.guild_only()
    @app_commands.describe(channel="Channel to rename", text="Base name (plain text)", style="Which font")
    @app_commands.choices(style=_STYLE_APP_CHOICES)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def stylerename(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           text: str, style: app_commands.Choice[str]):
        styled_name = apply_style(text[:80], style.value)
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.manage_channels:
            await interaction.response.send_message(
                "I don't have **Manage Channels** permission there \u2014 grant it and try again.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.edit(name=styled_name[:100], reason=f"Styled via /stylerename by {interaction.user}")
        except discord.HTTPException as e:
            await interaction.followup.send(f"\u274c Discord rejected that name: {e.text}", ephemeral=True)
            return
        await interaction.followup.send(f"\u2705 Renamed {channel.mention} to **{styled_name}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StyleCog(bot))
