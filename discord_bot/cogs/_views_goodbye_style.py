# path: discord_bot/cogs/_views_goodbye_style.py
"""Premium goodbye style editor: title, colour, banner, footer and avatar, with a live preview of the real post.

Two doors lead here and share this one editor, so they can never drift apart: the 👋 Goodbye panel inside the welcome
wizard, and the Server Owners Panel's "Goodbye & roles" screen. Free servers who tap Customize see the Premium pitch
instead. Every write re-checks Premium on the server side (a stale button can't write), goes through the same audited
helper as the rest of the goodbye settings, and only the person who opened the editor can use it.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord

from database import db
from discord_bot.cogs import welcome_extras as we
from modules import goodbye_style as gs
from modules import server_panel as sp

logger = logging.getLogger(__name__)

PREVIEW_NOTE = "-# Preview of the real post (your own name is used). Tap **Test goodbye** to post it in your goodbye channel."


async def gate_or_pitch(interaction: discord.Interaction, guild_id: int, clone_id) -> bool:
    """True when this server is Premium. Otherwise shows the Premium pitch (the interaction must already be
    deferred ephemeral) and returns False."""
    if await we.goodbye_premium(guild_id, clone_id):
        return True
    try:
        from discord_bot.cogs._views_premium import send_premium_pitch
        await send_premium_pitch(interaction, guild_id, clone_id)
    except Exception:
        logger.exception("[goodbye-style] couldn't show the Premium pitch for guild %s", guild_id)
        await interaction.followup.send("💎 Goodbye styling is a **Premium** feature. Run `/premium` to see the plans.",
                                        ephemeral=True)
    return False


async def open_style_editor(interaction: discord.Interaction, guild_id: int, clone_id, opener_id: int) -> None:
    """Sends the editor as a private message. Call AFTER interaction.response.defer(ephemeral=True)."""
    extras = await db.get_welcome_extras(guild_id, clone_id)
    view = GoodbyeStyleView(guild_id, clone_id, opener_id, extras)
    await interaction.followup.send(view=view, ephemeral=True, **view.payload(interaction))


class GoodbyeStyleView(discord.ui.View):
    def __init__(self, guild_id: int, clone_id, opener_id: int, extras: dict):
        super().__init__(timeout=900)
        self.guild_id, self.clone_id, self.opener_id, self.extras = guild_id, clone_id, opener_id, extras or {}
        current = self.extras.get("goodbye_color")
        current = gs.DEFAULT_COLOR if current is None else int(current)
        options = [discord.SelectOption(label=label, value=str(colour), emoji=emoji, default=colour == current)
                   for label, emoji, colour in gs.PRESETS]
        options.append(discord.SelectOption(label="Custom colour…", value="custom", emoji="🎨",
                                            description="Type a hex code like #ff8800"))
        sel = discord.ui.Select(placeholder="Pick a colour", options=options, min_values=1, max_values=1, row=0)
        sel.callback = self._color
        self.add_item(sel)
        show = gs.effective(self.extras, True)["show_avatar"]
        for label, emoji, style, cb in (
            ("Title", "🏷️", discord.ButtonStyle.primary, self._title),
            ("Banner", "🖼️", discord.ButtonStyle.primary, self._banner),
            ("Footer", "📝", discord.ButtonStyle.primary, self._footer),
            ("Avatar: shown" if show else "Avatar: hidden", "🙂" if show else "🙈", discord.ButtonStyle.secondary, self._avatar),
        ):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=1)
            b.callback = cb
            self.add_item(b)
        for label, emoji, style, cb in (
            ("Test goodbye", "🧪", discord.ButtonStyle.success, self._test),
            ("Reset style", "♻️", discord.ButtonStyle.danger, self._reset),
        ):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=2)
            b.callback = cb
            self.add_item(b)

    # ── rendering ────────────────────────────────────────────────────────
    def payload(self, interaction: discord.Interaction) -> dict:
        """content + embed for the editor message: the current choices and a preview of the real goodbye post."""
        head = "### 🎨 Goodbye style 💎 Premium\n" + "\n".join(gs.summary_lines(self.extras))
        guild = interaction.client.get_guild(self.guild_id)
        out: dict = {"content": head + "\n" + PREVIEW_NOTE}
        if guild is not None:
            try:
                out["embed"] = we.build_goodbye_embed(self.extras, interaction.user, guild, premium=True)
            except Exception:
                logger.exception("[goodbye-style] preview failed for guild %s", self.guild_id)
        return out

    async def save(self, interaction: discord.Interaction, **fields) -> None:
        """Writes the change (audited), then redraws this message. The interaction must already be deferred."""
        if not await we.goodbye_premium(self.guild_id, self.clone_id):
            await interaction.followup.send("💎 This server isn't Premium any more, so styling is locked. "
                                            "Your saved choices are kept and come back when Premium returns.",
                                            ephemeral=True)
            return
        await sp.set_welcome_extras(self.guild_id, self.clone_id, interaction.user.id, **fields)
        extras = await db.get_welcome_extras(self.guild_id, self.clone_id)
        view = GoodbyeStyleView(self.guild_id, self.clone_id, self.opener_id, extras)
        await interaction.edit_original_response(view=view, **view.payload(interaction))

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("Only the person who opened this can use it.", ephemeral=True)
            return False
        return True

    async def _open_modal(self, interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
        # Premium is re-checked before the form opens too, so a lapsed server doesn't type something that can't save.
        if not await we.goodbye_premium(self.guild_id, self.clone_id):
            await interaction.response.send_message("💎 Goodbye styling needs Premium on this server.", ephemeral=True)
            return
        await interaction.response.send_modal(modal)

    # ── controls ─────────────────────────────────────────────────────────
    async def _color(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        value = interaction.data["values"][0]
        if value == "custom":
            await self._open_modal(interaction, _ColorModal(self))
            return
        await interaction.response.defer()
        await self.save(interaction, goodbye_color=int(value))

    async def _title(self, interaction: discord.Interaction):
        if await self._allowed(interaction):
            await self._open_modal(interaction, _TextModal(self, "Goodbye title", "Title ({name} {guild}; blank = default)",
                                                           "goodbye_title", gs.TITLE_MAX, gs.DEFAULT_TITLE))

    async def _footer(self, interaction: discord.Interaction):
        if await self._allowed(interaction):
            await self._open_modal(interaction, _TextModal(self, "Goodbye footer", "Footer ({name} {guild}; blank = none)",
                                                           "goodbye_footer", gs.FOOTER_MAX, "See you around!"))

    async def _banner(self, interaction: discord.Interaction):
        if await self._allowed(interaction):
            await self._open_modal(interaction, _BannerModal(self))

    async def _avatar(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await self.save(interaction, goodbye_show_avatar=not gs.effective(self.extras, True)["show_avatar"])

    async def _reset(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await self.save(interaction, goodbye_title=None, goodbye_color=None, goodbye_image_url=None,
                        goodbye_footer=None, goodbye_show_avatar=True)

    async def _test(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.followup.send("⚠️ I'm not in that server any more.", ephemeral=True)
            return
        member = guild.get_member(interaction.user.id) or interaction.user
        ok, msg = await we.send_test_goodbye(interaction.client, guild, member)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True)


# ── forms ────────────────────────────────────────────────────────────────

class _TextModal(discord.ui.Modal):
    def __init__(self, view: GoodbyeStyleView, title: str, label: str, field: str, max_len: int, placeholder: str):
        super().__init__(title=title)
        self.view_, self.field = view, field
        self.text = discord.ui.TextInput(label=label[:45], style=discord.TextStyle.short, required=False,
                                         max_length=max_len, placeholder=placeholder,
                                         default=(view.extras.get(field) or "")[:max_len])
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view_.save(interaction, **{self.field: str(self.text.value or "").strip() or None})


class _BannerModal(discord.ui.Modal, title="Goodbye banner"):
    def __init__(self, view: GoodbyeStyleView):
        super().__init__()
        self.view_ = view
        self.url = discord.ui.TextInput(label="Image link (https, png/jpg/gif/webp)", style=discord.TextStyle.short,
                                        required=False, max_length=gs.URL_MAX, placeholder="https://… (blank = no banner)",
                                        default=view.extras.get("goodbye_image_url") or "")
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction):
        ok, url = gs.check_image_url(str(self.url.value or ""))
        if not ok:
            await interaction.response.send_message(
                "That doesn't look like an image link. It must start with **https://** and end in "
                ".png, .jpg, .gif or .webp.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.view_.save(interaction, goodbye_image_url=url)


class _ColorModal(discord.ui.Modal, title="Goodbye colour"):
    def __init__(self, view: GoodbyeStyleView):
        super().__init__()
        self.view_ = view
        self.hex = discord.ui.TextInput(label="Hex colour", style=discord.TextStyle.short, required=True,
                                        max_length=7, placeholder="#ff8800",
                                        default=gs.color_hex(view.extras.get("goodbye_color")))
        self.add_item(self.hex)

    async def on_submit(self, interaction: discord.Interaction):
        colour: Optional[int] = gs.parse_color(str(self.hex.value or ""))
        if colour is None:
            await interaction.response.send_message("That isn't a hex colour. Try something like **#ff8800**.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.view_.save(interaction, goodbye_color=colour)
