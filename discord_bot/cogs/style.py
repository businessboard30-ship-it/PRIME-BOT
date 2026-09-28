# path: discord_bot/cogs/style.py

"""/style — opens straight into ONE wizard panel. No typing required:

  1. pick a channel (its current name becomes the text),
  2. pick a font (every option previews your text in that font),
  3. hit Apply.

The panel shows your choices + a live preview of the final name as you go.
Text only comes from a modal if you tap "Edit text" (or "AI ideas") — never
from a slash-command text box. /style's options (text/font/channel) are all
optional; they just prefill the wizard, or (font + channel) apply directly,
which is also the path the AI chat uses.

Discord channel names are lowercase, so text is lowercased before the font
is applied (and spaces become hyphens on text/forum channels) — see
modules.text_styles.channel_name.

The channel-name wizard reachable from the join DM's "styles" button
(_views_style_wizard.py) is a separate entry point and is untouched here.

AI chat: 'style' is in modules/ai_command_allowlist.py; a call that passes
font/channel needs the Confirm click (AI_CONFIRMED_HANDLERS /
AI_CONFIRM_PROMPTS below).
"""

import asyncio
import logging
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from modules.text_styles import (
    BRACKETS, FREE_BRACKETS, FREE_FONTS, STYLES, all_style_choices, channel_name,
    free_bracket_choices, free_style_choices, is_premium_style, premium_bracket_choices,
    premium_style_choices,
)
import config
from database import db
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

_RENAMABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel,
              discord.CategoryChannel)
_CHANNEL_TYPES = [
    discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice,
    discord.ChannelType.stage_voice, discord.ChannelType.forum,
]
_CATEGORY_TYPES = [discord.ChannelType.category]
AI_PROMPT_CONTEXT_CATEGORY = (
    "You are suggesting Discord CATEGORY names. The user will describe a theme or purpose for a "
    "category. Reply with EXACTLY 5 short category-name ideas, one per line, each 1-3 words, in "
    "UPPERCASE with spaces (e.g. 'COMMUNITY HUB'), each prefixed with a single fitting emoji. "
    "No numbering, no extra commentary, no markdown \u2014 just the 5 lines."
)
_MAX_TEXT = 60


def _hyphenate(channel) -> bool:
    """Text/forum channels can't hold spaces; voice/stage/categories can."""
    return not isinstance(channel, (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel))


def _name(text: str, key: str, channel=None, bracket=None, mode: str = "channel") -> str:
    """The one place a styled name is built. Categories keep their case and
    spaces (Discord only lowercases/hyphenates text-type channels)."""
    is_cat = isinstance(channel, discord.CategoryChannel) if channel else (mode == "category")
    hy = _hyphenate(channel) if channel else (mode != "category")
    return channel_name(text, key, hy, bracket, lower=not is_cat)


def _md(text: str) -> str:
    return text.replace("`", "'")


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


def _normalize_bracket(value) -> Optional[str]:
    """Bracket key or label -> key. 'none'/empty -> None. Unknown -> False."""
    if value in (None, ""):
        return None
    raw = str(value).strip().lower()
    key = raw.replace(" ", "_").replace("-", "_")
    if key in ("none", "no", "no_brackets"):
        return None
    if key in BRACKETS:
        return key
    for k, (label, left, right) in BRACKETS.items():
        if raw in (label.lower(), left, (left + right)):
            return k
    return False


async def _premium_flags(user, guild, clone_id) -> tuple:
    """(premium_active, can_manage_server) for this user in this guild."""
    try:
        active = bool(await db.is_guild_premium_active(guild.id, clone_id))
    except Exception:
        logger.exception("premium check failed")
        active = False
    perms = getattr(user, "guild_permissions", None)
    can = bool(perms and (perms.manage_guild or perms.administrator)) or (guild.owner_id == user.id)
    return active, can


def _premium_block_message(font, bracket, active: bool, can_manage: bool) -> Optional[str]:
    """None when this style is allowed; otherwise why it isn't."""
    if not is_premium_style(font, bracket):
        return None
    if not active:
        return f"\U0001f48e That style is **Premium** \u2014 Go Premium (${config.PREMIUM_FEE_USD:g}/month per server) to unlock it."
    if not can_manage:
        return "\U0001f48e Premium styles need the **Manage Server** permission."
    return None


async def _rename(user, guild, channel, new_name: str) -> tuple:
    """(ok, message). The one place a rename actually happens — re-checks the
    USER's and the BOT's Manage Channels on THAT channel."""
    if not isinstance(channel, _RENAMABLE):
        return False, "\u274c I can only rename text, voice, stage, forum channels and categories."
    if not channel.permissions_for(user).manage_channels:
        return False, f"\u274c You need **Manage Channels** on {channel.mention} to rename it."
    if not channel.permissions_for(guild.me).manage_channels:
        return False, f"\u274c I don't have **Manage Channels** on {channel.mention} \u2014 grant it and try again."
    if not new_name.strip():
        return False, "\u274c Nothing to rename it to."
    try:
        # Discord allows only 2 renames per 10 min per channel. discord.py
        # silently sleeps out that limit (up to ~10 min), which left the
        # wizard stuck on "Renaming..." — so give up fast and say why.
        await asyncio.wait_for(
            channel.edit(name=new_name[:100], reason=f"Styled via /style by {user}"), timeout=10,
        )
    except asyncio.TimeoutError:
        return False, (
            f"\u23f3 Discord is rate-limiting renames on {channel.mention} (max 2 every 10 minutes). "
            "Nothing was changed \u2014 wait a few minutes and tap Apply again."
        )
    except discord.HTTPException as e:
        return False, f"\u274c Discord rejected that name: {e.text}"
    return True, f"\u2705 {channel.mention} renamed to **{new_name[:100]}**"


# ── wizard components ───────────────────────────────────────────────────────

class _WizChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wiz: "_StyleWizard"):
        defaults = ([discord.SelectDefaultValue(id=wiz.channel.id, type=discord.SelectDefaultValueType.channel)]
                    if wiz.channel else [])
        super().__init__(
            placeholder=("1 \u00b7 Pick the category to rename\u2026" if wiz.mode == "category"
                         else "1 \u00b7 Pick the channel to rename\u2026"),
            channel_types=(_CATEGORY_TYPES if wiz.mode == "category" else _CHANNEL_TYPES), min_values=1, max_values=1, default_values=defaults,
        )
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        wiz = self.wiz
        picked = self.values[0]
        real = interaction.guild.get_channel(picked.id) if interaction.guild else None
        if real is None or not isinstance(real, _RENAMABLE):
            wiz.status = "\u274c I can't see that channel."
        elif isinstance(real, discord.CategoryChannel) != (wiz.mode == "category"):
            wiz.status = "\u274c That's the wrong kind \u2014 use the Categories / Channels button to switch."
        elif not real.permissions_for(interaction.user).manage_channels:
            wiz.status = f"\u274c You need **Manage Channels** on {real.mention} to rename it."
        else:
            wiz.channel = real
            wiz.status = None
        wiz.rebuild()
        await interaction.response.edit_message(view=wiz)


class _WizFontSelect(discord.ui.Select):
    def __init__(self, wiz: "_StyleWizard", premium: bool):
        base = wiz.base_text
        choices = premium_style_choices() if premium else free_style_choices()
        options = [
            discord.SelectOption(
                label=label, value=key, default=(key == wiz.font),
                # Live preview of the user's own text in each font. NO emoji=
                # (Discord rejects non-emoji glyphs there with a 400).
                description=(_name(base, key, wiz.channel, wiz.bracket, wiz.mode)[:100] or None) if base else None,
            )
            for key, label, _sample in choices
        ]
        super().__init__(
            placeholder=("2 \u00b7 \U0001f48e Premium fonts\u2026" if premium else "2 \u00b7 Pick a font\u2026"),
            options=options, min_values=1, max_values=1,
        )
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        self.wiz.font = self.values[0]
        self.wiz.status = None
        self.wiz.rebuild()
        await interaction.response.edit_message(view=self.wiz)


class _WizBracketSelect(discord.ui.Select):
    def __init__(self, wiz: "_StyleWizard", premium: bool):
        base = wiz.base_text
        font = wiz.font or "bold"
        choices = premium_bracket_choices() if premium else free_bracket_choices()
        options = [
            discord.SelectOption(
                label=label, value=key, default=(key == (wiz.bracket or "none")),
                description=(_name(base, font, wiz.channel, key, wiz.mode)[:100] or None) if base else None,
            )
            for key, label in choices
        ]
        super().__init__(
            placeholder=("3 \u00b7 \U0001f48e Premium brackets & designs\u2026" if premium
                         else "3 \u00b7 Brackets (optional)\u2026"),
            options=options, min_values=1, max_values=1,
        )
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        self.wiz.bracket = None if v == "none" else v
        self.wiz.status = None
        self.wiz.rebuild()
        await interaction.response.edit_message(view=self.wiz)


class _WizIdeaSelect(discord.ui.Select):
    def __init__(self, wiz: "_StyleWizard"):
        super().__init__(
            placeholder="\u2728 Pick an AI idea to use as the text\u2026",
            options=[discord.SelectOption(label=idea[:100], value=str(i)) for i, idea in enumerate(wiz.ideas)],
        )
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        if 0 <= idx < len(self.wiz.ideas):
            self.wiz.text = self.wiz.ideas[idx][:_MAX_TEXT]
        self.wiz.ideas = []
        self.wiz.status = None
        self.wiz.rebuild()
        await interaction.response.edit_message(view=self.wiz)


class _WizTextModal(discord.ui.Modal, title="Edit the text"):
    def __init__(self, wiz: "_StyleWizard"):
        super().__init__()
        self.wiz = wiz
        self.field = discord.ui.TextInput(
            label="Text to style (lowercased for you)", style=discord.TextStyle.short,
            max_length=_MAX_TEXT, required=True, default=wiz.base_text[:_MAX_TEXT],
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.wiz.text = str(self.field.value).strip()[:_MAX_TEXT] or None
        self.wiz.status = None
        self.wiz.rebuild()
        await interaction.response.edit_message(view=self.wiz)


class _WizAIModal(discord.ui.Modal, title="AI channel-name ideas"):
    def __init__(self, wiz: "_StyleWizard"):
        super().__init__()
        self.wiz = wiz
        self.theme = discord.ui.TextInput(
            label="What's the channel about?", style=discord.TextStyle.short,
            max_length=200, required=True, placeholder="e.g. sharing funny anime clips",
        )
        self.add_item(self.theme)

    async def on_submit(self, interaction: discord.Interaction):
        wiz = self.wiz
        await interaction.response.defer()  # AI call can take >3s
        user_id = interaction.user.id
        tier = await get_user_tier(user_id)
        allowed, warning = await check_ai_usage_limit(user_id, tier, "messages")
        if not allowed:
            wiz.status = f"\u274c {warning}"
        else:
            reply = await ai_chat(
                user_id, str(self.theme.value), tier=tier,
                command_context=(AI_PROMPT_CONTEXT_CATEGORY if wiz.mode == "category" else AI_PROMPT_CONTEXT), guild_id=wiz.guild_id, kind="style_wizard",
            )
            ideas = [ln.strip("-\u2022 ") for ln in (reply or "").splitlines() if ln.strip()][:5]
            if not reply:
                wiz.status = "\u274c AI is having trouble right now \u2014 try again shortly."
            elif not ideas:
                wiz.status = "Couldn't parse a suggestion out of that \u2014 try rephrasing."
            else:
                wiz.ideas = ideas
                wiz.status = "\u2728 Ideas ready \u2014 pick one below to use as the text."
        wiz.rebuild()
        await interaction.edit_original_response(view=wiz)


class _WizEditTextButton(discord.ui.Button):
    def __init__(self, wiz):
        super().__init__(label="Edit text", emoji="\u270f\ufe0f", style=discord.ButtonStyle.secondary)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_WizTextModal(self.wiz))


class _WizAIButton(discord.ui.Button):
    def __init__(self, wiz):
        super().__init__(label="AI ideas", emoji="\u2728", style=discord.ButtonStyle.secondary)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_WizAIModal(self.wiz))


class _WizAllFontsButton(discord.ui.Button):
    def __init__(self, wiz):
        super().__init__(label="All fonts", emoji="\U0001f440", style=discord.ButtonStyle.secondary)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        base = self.wiz.base_text
        if not base:
            self.wiz.status = "Pick a channel (or Edit text) first, then I can show every font."
            self.wiz.rebuild()
            await interaction.response.edit_message(view=self.wiz)
            return
        w = self.wiz
        lines = [f"{'\U0001f48e ' if key not in FREE_FONTS else ''}**{label}** \u2014 {_name(base, key, w.channel, w.bracket, w.mode)}" for key, label, _s in all_style_choices()]
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for extra in chunks[1:]:
            await interaction.followup.send(extra, ephemeral=True)


class _WizModeButton(discord.ui.Button):
    def __init__(self, wiz):
        cat = wiz.mode == "category"
        super().__init__(label="Channels" if cat else "Categories",
                         emoji="#\ufe0f\u20e3" if cat else "\U0001f4c1", style=discord.ButtonStyle.primary)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        wiz = self.wiz
        wiz.mode = "channel" if wiz.mode == "category" else "category"
        wiz.channel = None
        wiz.ideas = []
        wiz.status = None
        wiz.rebuild()
        await interaction.response.edit_message(view=wiz)


class _WizGoPremiumButton(discord.ui.Button):
    def __init__(self, wiz):
        super().__init__(label=f"Go Premium \U0001f48e \u2014 ${config.PREMIUM_FEE_USD:g}/month",
                         style=discord.ButtonStyle.primary)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, self.wiz.guild_id, self.wiz.clone_id)


class _WizApplyButton(discord.ui.Button):
    def __init__(self, wiz):
        ready = bool(wiz.channel and wiz.base_text and wiz.font) and not wiz.locked
        super().__init__(label="Apply", emoji="\u2705", style=discord.ButtonStyle.success, disabled=not ready)
        self.wiz = wiz

    async def callback(self, interaction: discord.Interaction):
        wiz = self.wiz
        if not (wiz.channel and wiz.base_text and wiz.font):
            return
        # Re-check premium + Manage Server at click time (state may be stale).
        wiz.premium_active, wiz.can_manage = await _premium_flags(interaction.user, interaction.guild, wiz.clone_id)
        block = _premium_block_message(wiz.font, wiz.bracket, wiz.premium_active, wiz.can_manage)
        if block:
            wiz.status = block
            wiz.rebuild()
            await interaction.response.edit_message(view=wiz)
            return
        new_name = wiz.result_name()
        # Ack instantly + show progress; Discord rate-limits renames to
        # 2 per 10 min per channel, so the edit below can take a while.
        wiz.status = f"\u23f3 Renaming {wiz.channel.mention}\u2026"
        wiz.rebuild()
        await interaction.response.edit_message(view=wiz)
        ok, msg = await _rename(interaction.user, interaction.guild, wiz.channel, new_name)
        wiz.status = msg
        wiz.rebuild()
        await interaction.edit_original_response(view=wiz)


class _StyleWizard(discord.ui.LayoutView):
    """The whole /style experience — one panel, state shown live."""

    def __init__(self, guild_id: int, clone_id, *, channel=None, text: Optional[str] = None,
                 font: Optional[str] = None, bracket: Optional[str] = None,
                 premium_active: bool = False, can_manage: bool = False):
        super().__init__(timeout=600)
        self.premium_active = premium_active
        self.can_manage = can_manage
        self.mode = "category" if isinstance(channel, discord.CategoryChannel) else "channel"
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.channel = channel
        self.text = text          # None => use the channel's current name
        self.font = font
        self.bracket: Optional[str] = bracket
        self.ideas: list = []
        self.status: Optional[str] = None
        self.rebuild()

    @property
    def locked(self) -> bool:
        """A Premium style is picked but this user/server can't use it."""
        return _premium_block_message(self.font, self.bracket, self.premium_active, self.can_manage) is not None

    @property
    def base_text(self) -> str:
        if self.text:
            return self.text.strip()
        return self.channel.name if self.channel else ""

    def result_name(self) -> str:
        return _name(self.base_text, self.font, self.channel, self.bracket, self.mode)

    def rebuild(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Color.blurple())

        ch = self.channel.mention if self.channel else "*not picked yet*"
        if self.base_text:
            note = "" if self.text else " *(its current name)*"
            txt = f"`{_md(self.base_text)}`{note}"
        else:
            txt = "*pick a channel, or tap Edit text*"
        font = f"**{STYLES[self.font][0]}**" if self.font else "*not picked yet*"
        lines = [
            "## \U0001f524 Style a category" if self.mode == "category" else "## \U0001f524 Style a channel",
            f"**{'Category' if self.mode == 'category' else 'Channel'}** \u2014 {ch}",
            f"**Text** \u2014 {txt}",
            f"**Font** \u2014 {font}",
            f"**Brackets** \u2014 {BRACKETS[self.bracket][0] if self.bracket else '*none*'}",
        ]
        if self.base_text and self.font:
            lines += ["", "**Preview**", self.result_name()]
        lock_msg = _premium_block_message(self.font, self.bracket, self.premium_active, self.can_manage)
        if lock_msg and not self.status:
            lines += ["", lock_msg]
        if self.status:
            lines += ["", self.status]
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_WizChannelSelect(self)))
        container.add_item(discord.ui.ActionRow(_WizFontSelect(self, False)))
        container.add_item(discord.ui.ActionRow(_WizFontSelect(self, True)))
        container.add_item(discord.ui.ActionRow(_WizBracketSelect(self, False)))
        container.add_item(discord.ui.ActionRow(_WizBracketSelect(self, True)))
        if self.ideas:
            container.add_item(discord.ui.ActionRow(_WizIdeaSelect(self)))
        if self.locked and not self.premium_active:
            container.add_item(discord.ui.ActionRow(_WizGoPremiumButton(self)))
        container.add_item(discord.ui.ActionRow(
            _WizEditTextButton(self), _WizAIButton(self), _WizAllFontsButton(self), _WizModeButton(self),
            _WizApplyButton(self),
        ))
        container.add_item(discord.ui.TextDisplay(
            ("-# Category names keep your capitals." if self.mode == "category" else "-# Discord channel names are lowercase \u2014 your text is lowercased for you.")
            + "\n-# \u23f3 Discord allows only 2 renames per channel every 10 minutes \u2014 preview as much as you like, then Apply once."
        ))
        self.add_item(container)


# ── cog ─────────────────────────────────────────────────────────────────────

class StyleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # AI tool-calling: a /style call that passes font/channel renames a
    # channel, so ai_command_allowlist requires a Confirm click for it (a
    # bare call needs none). The Confirm click already used the
    # interaction's response, so the confirmed call runs ai_style
    # (followup-only), and ai_style_prompt writes the confirm text and
    # refuses early when it could never succeed.
    AI_CONFIRMED_HANDLERS = {"style": "ai_style"}
    AI_CONFIRM_PROMPTS = {"style": "ai_style_prompt"}

    def _ai_check(self, interaction, text, font, channel, bracket=None):
        """(error_or_None, text_or_None, style_key_or_None). text None means
        'use the channel's current name'."""
        if channel is None:
            return ("\u274c Which channel? Mention it (#channel), give its name, or say \u201cthis channel\u201d."), None, None
        if not isinstance(channel, _RENAMABLE):
            return "\u274c I can only rename text, voice, stage, forum channels and categories.", None, None
        style_key = None
        if font not in (None, ""):
            style_key = _normalize_font(font)
            if style_key is None:
                return f"\u274c I don't know a font called \u201c{font}\u201d.", None, None
        if _normalize_bracket(bracket) is False:
            return f"\u274c I don't know a bracket style called \u201c{bracket}\u201d.", None, style_key
        if not channel.permissions_for(interaction.user).manage_channels:
            return f"\u274c You need **Manage Channels** on {channel.mention} to rename it.", None, style_key
        if not channel.permissions_for(interaction.guild.me).manage_channels:
            return f"\u274c I don't have **Manage Channels** on {channel.mention}.", None, style_key
        text = (text or "").strip()[:_MAX_TEXT] or None
        return None, text, style_key

    async def ai_style_prompt(self, interaction, text=None, font=None, channel=None, bracket=None, **_ignored):
        err, text, style_key = self._ai_check(interaction, text, font, channel, bracket)
        if err:
            return False, err
        base = text or channel.name
        active, can = await _premium_flags(interaction.user, interaction.guild, getattr(self.bot, "clone_id", None))
        block = _premium_block_message(style_key, _normalize_bracket(bracket) or None, active, can)
        if block:
            return False, block
        if style_key is None:
            return True, f"Open the style wizard for {channel.mention} (text: **{base}**)?"
        new_name = _name(base, style_key, channel, _normalize_bracket(bracket) or None)
        return True, f"Rename {channel.mention} to **{new_name}** ({STYLES[style_key][0]})?"

    async def ai_style(self, interaction, text=None, font=None, channel=None, bracket=None, **_ignored):
        err, text, style_key = self._ai_check(interaction, text, font, channel, bracket)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        clone_id = getattr(self.bot, "clone_id", None)
        active, can = await _premium_flags(interaction.user, interaction.guild, clone_id)
        block = _premium_block_message(style_key, _normalize_bracket(bracket) or None, active, can)
        if block:
            await interaction.followup.send(block, ephemeral=True)
            return
        if style_key is None:
            wiz = _StyleWizard(interaction.guild.id, clone_id, channel=channel, text=text,
                               bracket=_normalize_bracket(bracket) or None,
                               premium_active=active, can_manage=can)
            await interaction.followup.send(view=wiz, ephemeral=True)
            return
        new_name = _name(text or channel.name, style_key, channel, _normalize_bracket(bracket) or None)
        _ok, msg = await _rename(interaction.user, interaction.guild, channel, new_name)
        await interaction.followup.send(f"{msg} ({STYLES[style_key][0]})" if _ok else msg, ephemeral=True)

    @app_commands.command(
        name="style",
        description="Restyle a channel's name with fancy fonts \u2014 opens a wizard",
    )
    async def style(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return
        clone_id = getattr(self.bot, "clone_id", None)
        active, can = await _premium_flags(interaction.user, interaction.guild, clone_id)
        wiz = _StyleWizard(interaction.guild.id, clone_id, premium_active=active, can_manage=can)
        await interaction.response.send_message(view=wiz, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StyleCog(bot))
