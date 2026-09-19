# path: discord_bot/cogs/_views_card_customize.py

"""
Customize Card (ULTRA card) wizard — opened by the "Customize Card" button in
the /welcome setup wizard (WelcomeUltraPackButton in _views_welcome.py).

"Customize Card" is the ultra pack: the server supplies its OWN background
image and the bot draws the welcome card over it (see
modules/welcome_card._draw_custom_bg_card). This wizard is where that card is
designed — one ephemeral message (only the admin who tapped sees it):

  - background   upload a png/jpg or paste a direct link (+ clear)
  - banner       bottom / top / none
  - darkness     light / medium / heavy
  - avatar side  left / right, and avatar frame shape
  - text color   white, gold, cyan, pink, green, red
  - heading      replace the "Welcome to {server}!" line
  - member #     show/hide the "MEMBER #N" line
  - preview      renders the real card (a sample backdrop is used until a
                 background is set); reset puts the layout back to default

Servers that haven't bought Customize Card see a short pitch + an Unlock
button (same payment flow as /welcome buyultra) instead.

Layout options are stored as JSON in discord_welcome_config.ultra_card_json
and validated by modules.welcome_card.parse_ultra_options.

Same architecture as _views_welcome.py: every component is a
discord.ui.DynamicItem that re-reads the DB itself (nothing goes stale,
survives restarts). Registered via DYNAMIC_ITEMS in bot.py's setup_hook.
Import direction: this module imports _views_welcome at module level;
_views_welcome imports THIS module only lazily inside a callback.
"""

import asyncio
import io
import json
import logging
import re

import aiohttp
import discord
from PIL import Image, ImageDraw

import config as bot_config
from database import db
from discord_bot.cogs._views_shared import check_wizard_access
from discord_bot.cogs import _views_welcome as vw
from modules.welcome_card import (
    ULTRA_AVATAR_SIDES, ULTRA_BANNERS, ULTRA_DIM_ALPHA, ULTRA_HEADING_MAX, ULTRA_TEXT_COLORS,
    TEMPLATE_HEIGHT, TEMPLATE_WIDTH, parse_ultra_options, render_welcome_card,
)

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


# ── option storage ────────────────────────────────────────────────────────

async def _save_option(guild_id: int, clone_id, **changes) -> None:
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    opts = parse_ultra_options(cfg.get("ultra_card_json"))
    opts.update(changes)
    await db.set_welcome_config(guild_id, clone_id=clone_id, ultra_card_json=json.dumps(opts))


# ── rendering the wizard ──────────────────────────────────────────────────

_BANNER_LABELS = {"bottom": "Bottom banner (classic)", "top": "Top banner", "none": "No banner (text over image)"}
_DIM_LABELS = {"light": "Light — see more of your image", "medium": "Medium (default)", "heavy": "Heavy — best text contrast"}
_SIDE_LABELS = {"left": "Avatar on the left (classic)", "right": "Avatar on the right"}
_COLOR_LABELS = {"white": "White (default)", "gold": "Gold", "cyan": "Cyan", "pink": "Pink", "green": "Green", "red": "Red"}


def _locked_view(guild_id: int, clone_id, invoker_id) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.gold())
    container.add_item(discord.ui.TextDisplay("\n".join([
        "### 🖼️ Customize Card",
        "Design your own welcome card: use **your own background image** and choose where the banner "
        "goes, how dark it is, which side the avatar sits on, the text color, and your own heading text.",
        f"One-time **${bot_config.ULTRA_PACK_FEE_USD:g}**, whole server, applies to every future join.",
    ])))
    container.add_item(discord.ui.Separator())
    row = discord.ui.ActionRow()
    row.add_item(CardUnlockButton(guild_id, clone_id, invoker_id))
    row.add_item(CardDoneButton(guild_id, clone_id, invoker_id))
    container.add_item(row)
    view.add_item(container)
    return view


def _status_lines(config: dict, opts: dict) -> list:
    has_bg = bool(config.get("custom_background_url"))
    shape = config.get("avatar_shape", "circle")
    heading = opts["heading"] or "Welcome to {server}! (default)"
    return [
        "### 🖼️ Customize your welcome card",
        ("✅ **Background:** your image" if has_bg
         else "▫️ **Background:** none yet — tap **Set background** (the preview uses a sample backdrop)"),
        f"📐 **Banner:** {opts['banner']} · darkness {opts['dim']}",
        f"🧑 **Avatar:** {opts['avatar_side']} side · {vw.AVATAR_SHAPE_LABELS.get(shape, shape).split(' — ')[0].lower()} frame",
        f"🎨 **Text color:** {opts['text_color']}",
        f"✍️ **Heading:** {heading}",
        f"🔢 **Member number:** {'shown' if opts['show_number'] else 'hidden'}",
        "-# Tap **Preview** to see the real card. Changes apply to the next welcome.",
    ]


def build_customize_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    if not config.get("ultra_pack_unlocked"):
        return _locked_view(guild_id, clone_id, invoker_id)

    opts = parse_ultra_options(config.get("ultra_card_json"))
    has_bg = bool(config.get("custom_background_url"))

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())
    container.add_item(discord.ui.TextDisplay("\n".join(_status_lines(config, opts))))
    container.add_item(discord.ui.Separator())

    for select_cls in (CardBannerSelect, CardDimSelect, CardSideSelect, CardShapeSelect, CardColorSelect):
        row = discord.ui.ActionRow()
        row.add_item(select_cls(guild_id, clone_id, invoker_id, config))
        container.add_item(row)

    container.add_item(discord.ui.Separator())
    bg_row = discord.ui.ActionRow()
    bg_row.add_item(CardBackgroundButton(guild_id, clone_id, invoker_id))
    if has_bg:
        bg_row.add_item(CardClearBackgroundButton(guild_id, clone_id, invoker_id))
    bg_row.add_item(CardHeadingButton(guild_id, clone_id, invoker_id))
    bg_row.add_item(CardNumberToggleButton(guild_id, clone_id, invoker_id, opts["show_number"]))
    container.add_item(bg_row)

    act_row = discord.ui.ActionRow()
    act_row.add_item(CardPreviewButton(guild_id, clone_id, invoker_id))
    act_row.add_item(CardResetButton(guild_id, clone_id, invoker_id))
    act_row.add_item(CardDoneButton(guild_id, clone_id, invoker_id))
    container.add_item(act_row)

    view.add_item(container)
    return view


async def open_customize_wizard(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the setup wizard's Customize Card button. The caller has
    already deferred (ephemeral) — this just posts the wizard as a followup."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    view = build_customize_view(guild_id, clone_id, interaction.user.id, config)
    await interaction.followup.send(view=view, ephemeral=True)


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    """Interaction must already be deferred. Also pushes the new state onto
    the public /welcome setup wizard so its status doesn't go stale."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    await interaction.edit_original_response(view=build_customize_view(guild_id, clone_id, invoker_id, config))
    try:
        await vw.refresh_posted_wizard(interaction.client, guild_id, clone_id)
    except Exception as e:  # best-effort
        logger.debug(f"[cardwz] main wizard refresh skipped: {e}")


async def _require_unlocked(interaction: discord.Interaction, guild_id: int, clone_id) -> bool:
    """Server-side gate for every write (the UI can be stale). Interaction
    must already be deferred/acked."""
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if cfg.get("ultra_pack_unlocked"):
        return True
    await interaction.followup.send("Customize Card isn't unlocked for this server.", ephemeral=True)
    return False


# ── option selects (one mixin, five thin subclasses) ──────────────────────

class _OptionSelectMixin:
    FIELD = ""        # key in ultra_card_json (or column) this select edits
    ID = ""           # custom_id field name — must match the class's template
    PLACEHOLDER = ""
    CHOICES: dict = {}

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = self._current(config)
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in self.CHOICES.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder=self.PLACEHOLDER, options=options,
            custom_id=_encode(self.ID, guild_id, clone_id, invoker_id),
        ))

    def _current(self, config: dict):
        return parse_ultra_options(config.get("ultra_card_json"))[self.FIELD]

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def _save(self, value: str):
        await _save_option(self.guild_id, self.clone_id, **{self.FIELD: value})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
            return
        await self._save(self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardBannerSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("banner")):
    FIELD, ID, PLACEHOLDER, CHOICES = "banner", "banner", "Banner position", _BANNER_LABELS


class CardDimSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("dim")):
    FIELD, ID, PLACEHOLDER, CHOICES = "dim", "dim", "Banner darkness", _DIM_LABELS


class CardSideSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("side")):
    FIELD, ID, PLACEHOLDER, CHOICES = "avatar_side", "side", "Avatar side", _SIDE_LABELS


class CardColorSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("color")):
    FIELD, ID, PLACEHOLDER, CHOICES = "text_color", "color", "Text color", _COLOR_LABELS


class CardShapeSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("shape")):
    """Avatar frame shape lives in its own column (shared with the other
    card modes), not in ultra_card_json."""
    FIELD, ID, PLACEHOLDER, CHOICES = "avatar_shape", "shape", "Avatar frame shape", vw.AVATAR_SHAPE_LABELS

    def _current(self, config: dict):
        return config.get("avatar_shape", "circle")

    async def _save(self, value: str):
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, avatar_shape=value)


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

        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
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


class _Btn:
    """Shared constructor/from_custom_id for the plain buttons."""
    FIELD = ""
    LABEL = ""
    STYLE = discord.ButtonStyle.secondary

    def __init__(self, guild_id: int, clone_id, invoker_id, *_):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=self.label_text(*_), style=self.STYLE,
            custom_id=_encode(self.FIELD, guild_id, clone_id, invoker_id),
        ))

    def label_text(self, *_):
        return self.LABEL

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)


class CardBackgroundButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("setbg")):
    FIELD, LABEL, STYLE = "setbg", "🖼️ Set background", discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        # The modal must be the first response (3s window); the unlocked
        # check happens again in on_submit, which is what gates the write.
        try:
            await interaction.response.send_modal(CardBackgroundModal(self.guild_id, self.clone_id, self.invoker_id))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open from the first tap
                raise


class CardClearBackgroundButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("clearbg")):
    FIELD, LABEL = "clearbg", "🗑️ Clear background"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
            return
        await db.set_welcome_config(
            self.guild_id, clone_id=self.clone_id,
            custom_background_url=None, custom_bg_channel_id=None, custom_bg_message_id=None,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardUnlockButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("unlock")):
    FIELD, STYLE = "unlock", discord.ButtonStyle.success

    def label_text(self, *_):
        return f"🔒 Unlock Customize Card (${bot_config.ULTRA_PACK_FEE_USD:g})"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if cfg.get("ultra_pack_unlocked"):
            await interaction.followup.send("✅ Already unlocked — tap **Customize Card** again to open the editor.", ephemeral=True)
            return
        from discord_bot.views_card_pack import start_ultra_pack_payment
        await start_ultra_pack_payment(interaction)


# ── heading / member number / reset ───────────────────────────────────────

class CardHeadingModal(discord.ui.Modal, title="Card heading text"):
    def __init__(self, guild_id: int, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.heading = discord.ui.TextInput(
            label="Heading ({guild} / {member} work; blank = default)",
            style=discord.TextStyle.short, required=False, max_length=ULTRA_HEADING_MAX,
            default=current or "", placeholder="Welcome to {guild}!",
        )
        self.add_item(self.heading)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
            return
        await _save_option(self.guild_id, self.clone_id, heading=str(self.heading.value or "").strip())
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardHeadingButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("heading")):
    FIELD, LABEL = "heading", "✍️ Heading text"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        cfg = await vw._get_config_for_modal(self.guild_id, self.clone_id)
        current = parse_ultra_options(cfg.get("ultra_card_json"))["heading"]
        try:
            await interaction.response.send_modal(CardHeadingModal(self.guild_id, self.clone_id, self.invoker_id, current))
        except discord.HTTPException as e:
            if e.code != 40060:
                raise


class CardNumberToggleButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("number")):
    FIELD = "number"

    def __init__(self, guild_id: int, clone_id, invoker_id, shown: bool = True):
        self._shown = shown
        super().__init__(guild_id, clone_id, invoker_id, shown)

    def label_text(self, shown=True, *_):
        return "🔢 Member #: on" if shown else "🔢 Member #: off"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
            return
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        current = parse_ultra_options(cfg.get("ultra_card_json"))["show_number"]
        await _save_option(self.guild_id, self.clone_id, show_number=not current)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardResetButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("reset")):
    FIELD, LABEL = "reset", "↩️ Reset layout"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        if not await _require_unlocked(interaction, self.guild_id, self.clone_id):
            return
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, ultra_card_json=None)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# ── preview / done ────────────────────────────────────────────────────────

def _sample_backdrop() -> bytes:
    """Stand-in image for previews before a background has been set."""
    img = Image.new("RGB", (TEMPLATE_WIDTH, TEMPLATE_HEIGHT))
    d = ImageDraw.Draw(img)
    for y in range(TEMPLATE_HEIGHT):
        t = y / TEMPLATE_HEIGHT
        d.line([(0, y), (TEMPLATE_WIDTH, y)], fill=(int(70 + 90 * t), int(90 + 40 * t), int(200 - 80 * t)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class CardPreviewButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("preview")):
    FIELD, LABEL, STYLE = "preview", "👁️ Preview", discord.ButtonStyle.primary

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
                bg_bytes = await _custom_bg_bytes_for_render(session, cfg, interaction.client)
            using_sample = bg_bytes is None
            if using_sample:
                bg_bytes = _sample_backdrop()
            card_bytes, _fmt = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, interaction.user.display_name, f"Member #{interaction.guild.member_count}",
                avatar_shape=cfg.get("avatar_shape", "circle"),
                guild_name=interaction.guild.name, use_template=True,
                custom_background_bytes=bg_bytes,
                ultra_options=cfg.get("ultra_card_json"),
            )
            note = "sample backdrop — set a background to use your own image" if using_sample else "your background"
            await interaction.followup.send(
                content=f"**Preview** ({note}) — only visible to you",
                file=discord.File(fp=io.BytesIO(card_bytes), filename="preview.png"),
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"[cardwz] preview failed for guild {self.guild_id}: {e}")
            await interaction.followup.send(f"Couldn't render a preview: {e}", ephemeral=True)


class CardDoneButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("done")):
    FIELD, LABEL = "done", "✅ Done"

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
    CardBannerSelect, CardDimSelect, CardSideSelect, CardColorSelect, CardShapeSelect,
    CardBackgroundButton, CardClearBackgroundButton, CardUnlockButton,
    CardHeadingButton, CardNumberToggleButton, CardResetButton, CardPreviewButton, CardDoneButton,
)
