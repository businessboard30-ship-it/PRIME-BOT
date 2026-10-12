# path: discord_bot/cogs/_views_level_card.py

"""Level-card designer inside Discord: `/levelcard` and the leaderboard's "🎨 My card" button (the AI can open it
too, it is on the AI allowlist). Same card, same rules as the website designer.

  * Needs the Level Pass (the `card_plan` entitlement). The check is made on the server on EVERY change, never
    from the buttons on screen. Without it the member only sees what the pass gives and a button to get it.
  * A design is data only: colours, font, shape, bio are validated by modules.level_card_design.
  * Uploads (background / logo) go through the same pipeline as the website (modules.card_assets.submit: size cap,
    PNG/JPEG only, re-encode, hash block-list, moderation). Only APPROVED images are ever drawn. An approved copy is
    also sent to the image-hosting channel the welcome backgrounds use (best effort, never blocks the upload).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re

import discord

import config as app_config
from database import db
from modules import card_assets, entitlements as ent, level_card_design as lcd

logger = logging.getLogger(__name__)

ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ACCEPTED = ("image/png", "image/jpeg")


# ── state ────────────────────────────────────────────────────────────────────
async def load_state(uid: int):
    """-> (has_pass, design, assets{kind: row}, expires_at|None). Read fresh every time."""
    rows = await db.entitlements_list(str(uid))
    has_pass = ent.has_access(rows, ("card_plan",))
    expires = next((r.get("expires_at") for r in rows if r.get("product") == "card_plan"), None)
    design = dict(lcd.DEFAULT_DESIGN)
    raw = await db.user_card_get(str(uid))
    if raw:
        try:
            parsed, _ = lcd.validate(json.loads(raw))
            design = parsed or design
        except Exception:
            pass
    assets = {}
    for kind in card_assets.KINDS:
        row = await db.card_asset_get(str(uid), kind)
        if row:
            assets[kind] = row
    return has_pass, design, assets, expires


def _approved(assets: dict, kind: str):
    a = assets.get(kind)
    return bytes(a["data"]) if a and a.get("status") == "approved" else None


async def _avatar_bytes(user, accent: str) -> bytes:
    try:
        return await user.display_avatar.replace(size=128, format="png").read()
    except Exception:
        return lcd.placeholder_avatar(accent)


async def render_preview(user, design: dict, assets: dict) -> discord.File:
    avatar = await _avatar_bytes(user, design.get("accent", "#57F287"))
    png = await asyncio.to_thread(
        lcd.render_custom_level_card, avatar, getattr(user, "display_name", "You"), 7, 40, 100, design,
        _approved(assets, "background"), _approved(assets, "logo"))
    return discord.File(io.BytesIO(png), filename="card.png")


def _asset_line(label: str, assets: dict, kind: str) -> str:
    a = assets.get(kind)
    if not a:
        return f"{label}: none"
    return f"{label}: {({'approved': '✅ approved', 'pending': '⏳ waiting for a check', 'rejected': '❌ rejected'}).get(a.get('status'), a.get('status'))}"


def build_embed(design: dict, assets: dict, expires) -> discord.Embed:
    e = discord.Embed(title="🎨 Your level-up card", color=discord.Color.from_str(design.get("accent", "#57F287")))
    bg = lcd.BACKGROUNDS.get(design.get("background"), ("", "", "Slate"))[2]
    until = f"\nLevel Pass active until <t:{int(expires.timestamp())}:D>" if expires else ""
    e.description = (
        f"Background **{bg}** · font **{lcd.FONT_LABELS.get(design.get('font'), '')}** · "
        f"shape **{lcd.SHAPES.get(design.get('shape'), '')}** · accent **{design.get('accent')}**\n"
        f"Bio: {design.get('bio') or '—'}\n"
        f"{_asset_line('Background image', assets, 'background')} · {_asset_line('Logo', assets, 'logo')}{until}")
    e.set_image(url="attachment://card.png")
    e.set_footer(text="Preview. Changes save as you make them and show on your next level-up.")
    return e


def pitch_embed() -> discord.Embed:
    return discord.Embed(
        title="🎫 Level Pass",
        description=(
            f"**${app_config.LEVEL_PASS_FEE_USD:g}, one payment, no subscription.**\n"
            f"• Design your own level-up card: colours, font, shape, bio, your own background and logo "
            f"({app_config.LEVEL_PASS_CARD_DAYS} days)\n"
            f"• **Plus** {app_config.XP_BOOST_MULTIPLIER:g}x XP for {app_config.XP_BOOST_DURATION_DAYS} days in this server\n\n"
            "Your design is kept if the pass runs out, and comes back when you renew."),
        color=discord.Color.gold())


# ── entry points ─────────────────────────────────────────────────────────────
async def open_card_wizard(interaction: discord.Interaction) -> None:
    """/levelcard, the leaderboard button and the AI all land here. Private to the person who asked."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        has_pass, design, assets, expires = await load_state(interaction.user.id)
        if not has_pass:
            from discord_bot.cogs._views_leveling_boost import pass_offered
            if await pass_offered(getattr(interaction.client, "clone_id", None)):
                await interaction.followup.send(embed=pitch_embed(), view=PitchView(interaction.guild_id), ephemeral=True)
            else:
                await interaction.followup.send("🎫 The Level Pass isn't available yet. Try again in a little while.", ephemeral=True)
            return
        file = await render_preview(interaction.user, design, assets)
        await interaction.followup.send(embed=build_embed(design, assets, expires), file=file,
                                        view=CardWizardView(interaction.user.id, design), ephemeral=True)
    except Exception:
        logger.exception("[level-card] couldn't open the designer")
        await interaction.followup.send("The card designer is temporarily unavailable.", ephemeral=True)


class PitchView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        btn = discord.ui.Button(label=f"Get the Level Pass — ${app_config.LEVEL_PASS_FEE_USD:g}", emoji="🎫",
                                style=discord.ButtonStyle.success)
        btn.callback = self._buy
        self.add_item(btn)

    async def _buy(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_leveling_boost import _start_boost_checkout
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _start_boost_checkout(interaction, "level_pass", self.guild_id)


# ── the designer ─────────────────────────────────────────────────────────────
async def _refresh(interaction: discord.Interaction) -> None:
    has_pass, design, assets, expires = await load_state(interaction.user.id)
    if not has_pass:
        await interaction.edit_original_response(embed=pitch_embed(), attachments=[], view=PitchView(interaction.guild_id))
        return
    file = await render_preview(interaction.user, design, assets)
    await interaction.edit_original_response(embed=build_embed(design, assets, expires), attachments=[file],
                                             view=CardWizardView(interaction.user.id, design))


async def apply_change(interaction: discord.Interaction, **changes) -> None:
    """Save one change for the person pressing the control, re-checking the pass on the server. Never raises."""
    try:
        await interaction.response.defer()
        has_pass, design, _assets, _exp = await load_state(interaction.user.id)
        if not has_pass:
            await _refresh(interaction)
            return
        new, err = lcd.validate({**design, **changes})
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await db.user_card_set(str(interaction.user.id), json.dumps(new, separators=(",", ":")))
        await _refresh(interaction)
    except Exception:
        logger.exception("[level-card] couldn't apply a change")
        try:
            await interaction.followup.send("Couldn't save that. Try again.", ephemeral=True)
        except Exception:
            pass


class _Pick(discord.ui.Select):
    def __init__(self, field: str, placeholder: str, labels: dict, current: str, row: int):
        super().__init__(placeholder=placeholder, row=row, options=[
            discord.SelectOption(label=label, value=key, default=(key == current)) for key, label in labels.items()])
        self.field = field

    async def callback(self, interaction: discord.Interaction):
        await apply_change(interaction, **{self.field: self.values[0]})


class CardWizardView(discord.ui.View):
    def __init__(self, owner_id: int, design: dict):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.add_item(_Pick("background", "Background", {k: v[2] for k, v in lcd.BACKGROUNDS.items()}, design["background"], 0))
        self.add_item(_Pick("font", "Font", lcd.FONT_LABELS, design["font"], 1))
        self.add_item(_Pick("shape", "Avatar shape", lcd.SHAPES, design["shape"], 2))
        for label, emoji, cb in (("Accent colour", "🎨", self._accent), ("Bio", "✏️", self._bio),
                                 ("Background image", "🖼️", self._bg), ("Logo", "🏷️", self._logo),
                                 ("Remove uploads", "🗑️", self._remove)):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=3)
            b.callback = cb
            self.add_item(b)
        self._design = design

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's card designer. Use /levelcard.", ephemeral=True)
            return False
        return True

    async def _accent(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AccentModal(self._design.get("accent", "#57F287")))

    async def _bio(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BioModal(self._design.get("bio", "")))

    async def _bg(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UploadModal("background"))

    async def _logo(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UploadModal("logo"))

    async def _remove(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            has_pass, design, _a, _e = await load_state(interaction.user.id)
            if has_pass:
                for kind in card_assets.KINDS:
                    await db.card_asset_delete(str(interaction.user.id), kind)
                new, _ = lcd.validate({**design, "custom_bg": False, "logo": False})
                await db.user_card_set(str(interaction.user.id), json.dumps(new, separators=(",", ":")))
            await _refresh(interaction)
        except Exception:
            logger.exception("[level-card] couldn't remove uploads")
            await interaction.followup.send("Couldn't remove that. Try again.", ephemeral=True)


class AccentModal(discord.ui.Modal, title="Accent colour"):
    colour = discord.ui.TextInput(label="Hex colour, like #57F287", min_length=6, max_length=7)

    def __init__(self, current: str):
        super().__init__()
        self.colour.default = current

    async def on_submit(self, interaction: discord.Interaction):
        v = (self.colour.value or "").strip()
        v = v if v.startswith("#") else "#" + v
        if not ACCENT_RE.match(v):
            await interaction.response.send_message("Use a hex colour like #57F287.", ephemeral=True)
            return
        await apply_change(interaction, accent=v)


class BioModal(discord.ui.Modal, title="Card bio"):
    bio = discord.ui.TextInput(label="Short bio (plain text)", required=False, max_length=lcd.BIO_MAX)

    def __init__(self, current: str):
        super().__init__()
        self.bio.default = current or None

    async def on_submit(self, interaction: discord.Interaction):
        await apply_change(interaction, bio=self.bio.value or "")


class UploadModal(discord.ui.Modal):
    """A real file picker (Discord's file-upload modal component). PNG or JPEG only; sized and checked again below."""

    def __init__(self, kind: str):
        super().__init__(title="Background image" if kind == "background" else "Logo")
        self.kind = kind
        self.file = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        hint = ("900x300 looks best. Keep the middle calm, text is drawn there." if kind == "background"
                else "Shown on the right side of the card.")
        self.add_item(discord.ui.Label(text="PNG or JPEG", description=hint, component=self.file))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            att = self.file.values[0]
            cap = card_assets.MAX_BYTES[self.kind]
            if (att.content_type or "").split(";")[0] not in ACCEPTED:
                await interaction.response.send_message("Only PNG or JPEG images, please.", ephemeral=True)
                return
            if att.size > cap:
                await interaction.response.send_message(f"That file is too big (max {cap // 1000} KB).", ephemeral=True)
                return
            await interaction.response.defer()
            has_pass, design, _a, _e = await load_state(interaction.user.id)
            if not has_pass:
                await _refresh(interaction)
                return
            raw = await att.read()
            res = await card_assets.submit(db, interaction.user.id, self.kind, raw)
            if not res["ok"]:
                await interaction.followup.send(res["message"], ephemeral=True)
                return
            flag = "custom_bg" if self.kind == "background" else "logo"
            new, _ = lcd.validate({**design, flag: True})
            await db.user_card_set(str(interaction.user.id), json.dumps(new, separators=(",", ":")))
            if res["status"] == "approved":
                await _send_to_host(interaction, self.kind)
            elif res["status"] == "rejected":
                await interaction.followup.send(f"That image can't be used: {res['reason']}", ephemeral=True)
            else:
                await interaction.followup.send("⏳ Saved. It shows on your card once it passes the automatic check.", ephemeral=True)
            await _refresh(interaction)
        except Exception:
            logger.exception("[level-card] upload failed")
            try:
                await interaction.followup.send("Couldn't upload that. Try again.", ephemeral=True)
            except Exception:
                pass


async def _send_to_host(interaction: discord.Interaction, kind: str) -> None:
    """Send the processed, APPROVED image to the same hosting channel the welcome backgrounds use. Best effort."""
    try:
        from modules import image_host
        row = await db.card_asset_get(str(interaction.user.id), kind)
        if row and row.get("status") == "approved":
            await image_host.post_to_host(
                interaction.client, bytes(row["data"]), f"Level card {kind} · user {interaction.user.id} · approved")
    except Exception:
        logger.warning("[level-card] couldn't copy the image to the hosting channel", exc_info=True)
