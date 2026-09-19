# path: discord_bot/cogs/_views_card_customize.py

"""
Customize Card wizard — opened by the "Customize Card" button in the
/welcome setup wizard (WelcomeUltraPackButton in _views_welcome.py).

One ephemeral message (only the admin who tapped the button sees it) that
covers everything that actually affects the designed welcome card:

  - card look          (wolf free; premium looks use the same 3-day trial /
                        lock rules as /welcome theme)
  - avatar frame shape
  - custom background  (upload a png/jpg or paste a direct link) — part of
                        Customize Card, so locked until the server has bought
                        it; the button then becomes "Unlock background" and
                        starts the same payment flow as /welcome buyultra
  - preview            (renders the real card, including a custom background)

Sticker / colors are deliberately not here: the template and custom-
background cards ignore them (see modules/welcome_card.py), so offering
them would be a control that does nothing.

Same architecture as _views_welcome.py: every component is a
discord.ui.DynamicItem that re-reads the DB itself, so nothing goes stale
and nothing depends on in-memory state. Registered via DYNAMIC_ITEMS in
bot.py's setup_hook.

Import direction: this module imports _views_welcome at module level;
_views_welcome imports THIS module only lazily inside a callback, so there
is no circular import.
"""

import asyncio
import io
import logging
import re

import aiohttp
import discord

import config as bot_config
from database import db
from discord_bot.cogs._views_shared import check_wizard_access
from discord_bot.cogs import _views_welcome as vw
from modules.welcome_card import PREMIUM_THEMES, render_welcome_card

logger = logging.getLogger(__name__)


# ── ids / access ──────────────────────────────────────────────────────────

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"cardwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    invoker_id = None if match.group(3) == "-" else int(match.group(3))
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^cardwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "welcome", "manage_guild", "Manage Server")


# ── rendering ─────────────────────────────────────────────────────────────

_LOOK_LABELS = dict(vw.WelcomeCardLookSelect._LOOKS)


def _status_lines(config: dict) -> list:
    unlocked = bool(config.get("ultra_pack_unlocked"))
    look = config.get("card_theme", "wolf")
    shape = config.get("avatar_shape", "circle")
    has_bg = bool(config.get("custom_background_url"))
    if has_bg and unlocked:
        bg_line = "✅ **Custom background:** set — it replaces the card look above"
    elif unlocked:
        bg_line = "▫️ **Custom background:** not set — tap **Set background**"
    else:
        bg_line = (
            f"🔒 **Custom background:** part of Customize Card "
            f"(${bot_config.ULTRA_PACK_FEE_USD:g} one-time, whole server)"
        )
    return [
        f"🎨 **Card look:** {_LOOK_LABELS.get(look, look)}",
        f"🖼️ **Avatar frame:** {vw.AVATAR_SHAPE_LABELS.get(shape, shape).split(' — ')[0]}",
        bg_line,
        "-# Changes apply to the next welcome card. Tap **Preview** to see the real thing.",
    ]


def build_customize_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    unlocked = bool(config.get("ultra_pack_unlocked"))
    has_bg = bool(config.get("custom_background_url"))

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    text = discord.ui.TextDisplay("\n".join(["### 🖼️ Customize your welcome card", *_status_lines(config)]))

    look_row = discord.ui.ActionRow()
    look_row.add_item(CardLookSelect(guild_id, clone_id, invoker_id, config))
    shape_row = discord.ui.ActionRow()
    shape_row.add_item(CardShapeSelect(guild_id, clone_id, invoker_id, config))

    button_row = discord.ui.ActionRow()
    if unlocked:
        button_row.add_item(CardBackgroundButton(guild_id, clone_id, invoker_id))
        if has_bg:
            button_row.add_item(CardClearBackgroundButton(guild_id, clone_id, invoker_id))
    else:
        button_row.add_item(CardUnlockButton(guild_id, clone_id, invoker_id))
    button_row.add_item(CardPreviewButton(guild_id, clone_id, invoker_id))
    button_row.add_item(CardDoneButton(guild_id, clone_id, invoker_id))

    for item in (text, discord.ui.Separator(), look_row, shape_row, discord.ui.Separator(), button_row):
        container.add_item(item)
    view.add_item(container)
    return view


async def open_customize_wizard(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the setup wizard's Customize Card button. The caller has
    already deferred (ephemeral) — this just posts the wizard as a
    followup."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    view = build_customize_view(guild_id, clone_id, interaction.user.id, config)
    await interaction.followup.send(view=view, ephemeral=True)


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    """Interaction must already be deferred (same rule as _views_welcome's
    _rerender). Also pushes the new state onto the public /welcome setup
    wizard so its status lines don't go stale."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    await interaction.edit_original_response(view=build_customize_view(guild_id, clone_id, invoker_id, config))
    try:
        await vw.refresh_posted_wizard(interaction.client, guild_id, clone_id)
    except Exception as e:  # best-effort
        logger.debug(f"[cardwz] main wizard refresh skipped: {e}")


# ── selects ───────────────────────────────────────────────────────────────

class CardLookSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("look")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("card_theme", "wolf")
        unlocked = bool(config.get("card_pack_unlocked"))
        trial_used = bool(config.get("card_pack_trial_used"))
        options = []
        for value, label in vw.WelcomeCardLookSelect._LOOKS:
            is_premium = value in PREMIUM_THEMES and not unlocked
            trial_available = is_premium and not trial_used
            locked = is_premium and not trial_available
            if trial_available:
                desc = "Free 3-day trial available"
            elif locked:
                desc = "Locked — preview only, /welcome buypack to use"
            else:
                desc = None
            options.append(discord.SelectOption(
                label=f"🔒 {label}" if locked else label, value=value, description=desc,
                default=(value == current),
            ))
        super().__init__(discord.ui.Select(
            placeholder="Card look", options=options,
            custom_id=_encode("look", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        chosen = self.item.values[0]
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        unlocked = bool(cfg.get("card_pack_unlocked"))

        if chosen in PREMIUM_THEMES and not unlocked:
            if not cfg.get("card_pack_trial_used"):
                await db.start_welcome_card_trial(self.guild_id, interaction.user.id, clone_id=self.clone_id)
                await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, card_theme=chosen, use_template=True)
                await interaction.followup.send(
                    "✅ This look is now active — free for **3 days** as a one-time trial. "
                    "Run `/welcome buypack` before then to keep it (and every premium look) for good.",
                    ephemeral=True,
                )
            else:
                # Same locked-preview behaviour as the setup wizard's look step.
                await vw.WelcomeCardLookSelect._send_locked_preview(None, interaction, chosen, cfg)
        else:
            await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, card_theme=chosen, use_template=True)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardShapeSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("shape")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("avatar_shape", "circle")
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in vw.AVATAR_SHAPE_LABELS.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder="Avatar frame shape", options=options,
            custom_id=_encode("shape", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, avatar_shape=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# ── custom background ─────────────────────────────────────────────────────

class CardBackgroundModal(discord.ui.Modal, title="Custom card background"):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.url = discord.ui.TextInput(
            style=discord.TextStyle.short, required=False, max_length=500,
            placeholder="https://…/image.png",
        )
        self.add_item(discord.ui.Label(
            text="Upload an image", description="png or jpg, up to 8MB", component=self.upload,
        ))
        self.add_item(discord.ui.Label(
            text="…or paste a direct image link", description="Fill in only one of the two", component=self.url,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from discord_bot.cogs.welcome import _upload_custom_bg, _fetch_custom_bg_bytes

        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if not cfg.get("ultra_pack_unlocked"):
            await interaction.followup.send("Custom backgrounds are part of Customize Card — unlock it first.", ephemeral=True)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return

        files = list(self.upload.values or [])
        url = str(self.url.value or "").strip()
        if files and url:
            await interaction.followup.send("⚠️ Use either an upload or a link, not both.", ephemeral=True)
            return
        if not files and not url:
            await interaction.followup.send("⚠️ Upload an image or paste a link first.", ephemeral=True)
            return

        if files:
            host_channel_id, host_message_id, cdn_url, reason = await _upload_custom_bg(
                interaction.client, files[0], interaction.guild
            )
            if reason:
                await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                return
            await db.set_welcome_config(
                self.guild_id, clone_id=self.clone_id,
                custom_background_url=cdn_url,
                custom_bg_channel_id=host_channel_id, custom_bg_message_id=host_message_id,
            )
        else:
            async with aiohttp.ClientSession() as session:
                data, reason = await _fetch_custom_bg_bytes(session, url)
            if data is None:
                await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                return
            await db.set_welcome_config(
                self.guild_id, clone_id=self.clone_id,
                custom_background_url=url, custom_bg_channel_id=None, custom_bg_message_id=None,
            )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardBackgroundButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("setbg")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="🖼️ Set background", style=discord.ButtonStyle.success,
            custom_id=_encode("setbg", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        # Modal must be the first response (3s window) — the unlocked check
        # happens again in on_submit, which is what actually gates the write.
        try:
            await interaction.response.send_modal(CardBackgroundModal(self.guild_id, self.clone_id, self.invoker_id))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open from the first tap
                raise


class CardClearBackgroundButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("clearbg")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="🗑️ Clear background", style=discord.ButtonStyle.secondary,
            custom_id=_encode("clearbg", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        await db.set_welcome_config(
            self.guild_id, clone_id=self.clone_id,
            custom_background_url=None, custom_bg_channel_id=None, custom_bg_message_id=None,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardUnlockButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("unlock")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=f"🔒 Unlock background (${bot_config.ULTRA_PACK_FEE_USD:g})", style=discord.ButtonStyle.success,
            custom_id=_encode("unlock", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if cfg.get("ultra_pack_unlocked"):
            # Bought since this wizard was opened — show the fresh state.
            await interaction.followup.send("✅ Already unlocked — reopen **Customize Card** to set your background.", ephemeral=True)
            return
        from discord_bot.views_card_pack import start_ultra_pack_payment
        await start_ultra_pack_payment(interaction)


# ── preview / done ────────────────────────────────────────────────────────

class CardPreviewButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("preview")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="👁️ Preview", style=discord.ButtonStyle.primary,
            custom_id=_encode("preview", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from discord_bot.cogs.welcome import _custom_bg_bytes_for_render
            cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    str(interaction.user.display_avatar.replace(size=256).url),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await vw._fetch_sticker_bytes(session, cfg.get("sticker_url"))
                bg_bytes = await _custom_bg_bytes_for_render(session, cfg, interaction.client)
            card_bytes, image_format = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, interaction.user.display_name, f"Member #{interaction.guild.member_count}",
                background_color=cfg.get("background_color", "#2b2d31"),
                accent_color=cfg.get("accent_color", "#5865F2"),
                sticker_bytes=sticker_bytes, animate=(cfg.get("card_style") == "gif"),
                avatar_shape=cfg.get("avatar_shape", "circle"),
                use_template=cfg.get("use_template", True),
                theme=cfg.get("card_theme", "wolf"),
                custom_background_bytes=bg_bytes,
            )
            ext = "gif" if image_format == "GIF" else "png"
            await interaction.followup.send(
                content="**Preview** — only visible to you",
                file=discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}"),
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"[cardwz] preview failed for guild {self.guild_id}: {e}")
            await interaction.followup.send(f"Couldn't render a preview: {e}", ephemeral=True)


class CardDoneButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("done")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✅ Done", style=discord.ButtonStyle.secondary,
            custom_id=_encode("done", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        done = discord.ui.LayoutView(timeout=None)
        done.add_item(discord.ui.Container(
            discord.ui.TextDisplay("✅ **Card customization saved.** Your next welcome card will use these settings."),
            accent_colour=discord.Color.green(),
        ))
        await interaction.edit_original_response(view=done)


DYNAMIC_ITEMS = (
    CardLookSelect, CardShapeSelect, CardBackgroundButton, CardClearBackgroundButton,
    CardUnlockButton, CardPreviewButton, CardDoneButton,
)
