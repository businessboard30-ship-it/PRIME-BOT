# path: discord_bot/cogs/_views_level_card.py

"""Level-card designer inside Discord: `/levelcard` and the leaderboard's "🎨 My card" button (the AI can open it
too, it is on the AI allowlist). Same card, same rules as the website designer.

  * Anyone can open the designer and edit freely with a live preview (nothing is stored). SAVING needs the Level Pass
    (the `card_plan` entitlement): the check is made on the server when Save is pressed, never from the buttons on
    screen. Without the pass, Save shows the pass offer instead.
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
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import discord

import config as app_config
from database import db
from modules import card_assets, entitlements as ent, level_card_design as lcd

logger = logging.getLogger(__name__)

ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ACCEPTED = ("image/png", "image/jpeg")


# ── state ────────────────────────────────────────────────────────────────────
async def load_state(uid: int, guild_id=None, clone_id=None):
    """-> (has_pass, design, assets{kind: row}, expires_at|None). Read fresh every time. In a Premium
    server `has_pass` is True without a Level Pass (see lcd.guild_card_access); the caller can tell the two apart by
    asking `_own_access(uid)`."""
    rows = await db.entitlements_list(str(uid))
    has_pass = ent.has_access(rows, ("card_plan",)) or await lcd.guild_card_access(db, guild_id, clone_id)
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


async def trial_state(uid: int) -> tuple:
    """-> (can start the free trial, is on the trial now). Read fresh; any failure means no trial offer."""
    try:
        rows = await db.entitlements_list(str(uid))
        return ent.trial_available(rows), ent.on_trial(rows)
    except Exception:
        return False, False


def _approved(assets: dict, kind: str):
    a = assets.get(kind)
    return bytes(a["data"]) if a and a.get("status") == "approved" else None


async def _avatar_bytes(user, accent: str) -> bytes:
    try:
        return await user.display_avatar.replace(size=128, format="png").read()
    except Exception:
        return lcd.placeholder_avatar(accent)


PREVIEW_STATS = (7, 40, 100)             # level, xp in level, xp needed: what the preview shows until "My real level" is on


@dataclass
class Draft:
    """What the person is editing. Lives only in the designer message (memory), never in the database until Save."""
    design: dict
    has_pass: bool = False
    expires: Any = None
    saved: dict = field(default_factory=dict)       # kind -> approved bytes already on the account
    staged: dict = field(default_factory=dict)      # kind -> (raw upload, processed image) picked but not saved yet
    real: Optional[tuple] = None                    # (level, xp in level, xp needed) from the member's real XP
    use_real: bool = False
    dirty: bool = False
    guild_id: Optional[int] = None
    scope: str = "user"                             # "user": the member's own card · "server": the whole server's card
    key: Optional[str] = None                       # where it is stored: None = the member, "g:<guild id>" = the server
    can_server: bool = False                        # Manage Server in a Premium server: the Server card switch is shown
    free_guild: bool = False                        # access comes from this server (Premium), not from a pass
    trial_available: bool = False                   # never had a card_plan row: Save offers the free trial
    on_trial: bool = False                          # the current access is the free trial

    def asset(self, kind: str):
        if kind in self.staged:
            return self.staged[kind][1]
        flag = "custom_bg" if kind == "background" else "logo"
        return self.saved.get(kind) if self.design.get(flag) else None

    def stats(self) -> tuple:
        return self.real if (self.use_real and self.real) else PREVIEW_STATS


async def draft_for(uid: int, guild_id=None, clone_id=None) -> Draft:
    has_pass, design, assets, expires = await load_state(uid, guild_id, clone_id)
    saved = {k: _approved(assets, k) for k in card_assets.KINDS if _approved(assets, k)}
    trial_ok, on_trial = await trial_state(uid)
    own = await _own_access(uid)
    return Draft(design=dict(design), has_pass=has_pass, expires=expires if own else None, saved=saved,
                 guild_id=guild_id, free_guild=has_pass and not own,
                 trial_available=trial_ok and not has_pass, on_trial=on_trial and own)


async def _own_access(uid: int) -> bool:
    """The member's own Level Pass / trial is active (not just this server's access)."""
    try:
        return ent.has_access(await db.entitlements_list(str(uid)), ("card_plan",))
    except Exception:
        return False


async def can_edit_server(interaction: discord.Interaction) -> bool:
    """Manage Server, in a server that has Premium. Checked when the switch is drawn AND again on every save."""
    perms = getattr(interaction.user, "guild_permissions", None)
    if not interaction.guild_id or not perms or not perms.manage_guild:
        return False
    return await lcd.guild_card_access(db, interaction.guild_id, getattr(interaction.client, "clone_id", None))


async def _server_draft(guild_id: int) -> Draft:
    key = lcd.server_key(guild_id)
    design = dict(lcd.DEFAULT_DESIGN)
    raw = await db.user_card_get(key)
    if raw:
        try:
            parsed, _ = lcd.validate(json.loads(raw))
            design = parsed or design
        except Exception:
            pass
    saved = {}
    for kind in card_assets.KINDS:
        row = await db.card_asset_get(key, kind)
        if row and row.get("status") == "approved":
            saved[kind] = bytes(row["data"])
    return Draft(design=design, has_pass=True, saved=saved, guild_id=guild_id, scope="server", key=key)


async def build_draft(interaction: discord.Interaction, scope: str = "user") -> Draft:
    """The draft for the member's own card, or (for someone who may) the server's card."""
    can = await can_edit_server(interaction)
    if scope == "server" and can:
        draft = await _server_draft(interaction.guild_id)
    else:
        draft = await draft_for(interaction.user.id, interaction.guild_id, getattr(interaction.client, "clone_id", None))
    draft.can_server = can
    return draft


async def render_preview(user, draft: Draft) -> discord.File:
    design = draft.design
    avatar = await _avatar_bytes(user, design.get("accent", "#57F287"))
    lvl, cur, need = draft.stats()
    # The preview is a smaller copy (about 1200 px max) of the same card, so big backgrounds stay quick to edit.
    # Full size is only drawn for level-up, /rank, My Rank and Share.
    png = await lcd.render_async(
        avatar, getattr(user, "display_name", "You"), lvl, cur, need, design,
        draft.asset("background"), draft.asset("logo"), send=False, fit_box=lcd.PREVIEW_BOX)
    return discord.File(io.BytesIO(png), filename="card.png")


def _asset_line(label: str, assets: dict, kind: str) -> str:
    a = assets.get(kind)
    if not a:
        return f"{label}: none"
    return f"{label}: {({'approved': '✅ approved', 'pending': '⏳ waiting for a check', 'rejected': '❌ rejected'}).get(a.get('status'), a.get('status'))}"


def _upload_line(draft: Draft, label: str, kind: str) -> str:
    if kind in draft.staged:
        return f"{label}: 🆕 picked, not saved yet"
    return f"{label}: {'✅ yours' if draft.asset(kind) else 'none'}"


def _hidden_line(design: dict) -> str:
    hidden = [lcd.SHOW_LABELS[k].split(" (")[0] for k in lcd.SHOW_KEYS if not design.get(k, True)]
    return f"Hidden: {', '.join(hidden)}\n" if hidden else ""


def build_embed(draft: Draft, notice: str = "") -> discord.Embed:
    design = draft.design
    e = discord.Embed(title="🎨 Your level-up card", color=discord.Color.from_str(design.get("accent", "#57F287")))
    bg = lcd.BACKGROUNDS.get(design.get("background"), ("", "", "Slate"))[2]
    if draft.scope == "server":
        e.title = "🏰 Your server's level-up card"
        status = ("🏰 **Server card.** Members of this server who haven't made their own card see this one. "
                  + ("Press **Save** to keep your changes." if draft.dirty else "Everything is saved."))
    elif draft.free_guild:
        status = ("🎫 **This server has Premium, so your card is free here.** "
                  + ("Press **Save** to keep your changes." if draft.dirty else "Everything is saved."))
    elif draft.has_pass:
        name = "Free trial" if draft.on_trial else "Level Pass"
        until = f"{name} active until <t:{int(draft.expires.timestamp())}:D>" if draft.expires else f"{name} active"
        status = f"🎫 {until}. Press **Save** to keep your changes." if draft.dirty else f"🎫 {until}. Everything is saved."
    elif draft.trial_available:
        status = (f"👀 **Preview mode.** Edit anything you like, then press **Save** to start your "
                  f"**free {app_config.LEVEL_CARD_TRIAL_DAYS}-day trial** (no payment).")
    else:
        status = (f"👀 **Preview mode.** Edit anything you like. Saving needs the Level Pass "
                  f"(${app_config.LEVEL_PASS_FEE_USD:g}).")
    e.description = (
        f"{status}\n\n"
        f"Background **{bg}** · font **{lcd.FONT_LABELS.get(design.get('font'), '')}** · "
        f"shape **{lcd.SHAPES.get(design.get('shape'), '')}** · accent **{design.get('accent')}**\n"
        f"Avatar **{lcd.AVATAR_POSITIONS.get(design.get('avatar_pos'), '')}** · "
        f"fade **{lcd.AVATAR_FADES.get(design.get('avatar_fade'), ('',))[0]}**\n"
        f"Bio: {design.get('bio') or '—'}\n"
        f"{_hidden_line(design)}"
        f"{_upload_line(draft, 'Background image', 'background')} · {_upload_line(draft, 'Logo', 'logo')}"
        + (f"\n\n{notice}" if notice else ""))
    e.set_image(url="attachment://card.png")
    shown = "your real level" if (draft.use_real and draft.real) else "a sample level"
    e.set_footer(text=f"Preview with {shown}. Images up to {card_assets.MAX_BYTES['background'] // (1024 * 1024)} MB. "
                      "Your background image sets the card's size and shape.")
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
    """/levelcard, the leaderboard button and the AI all land here. Private to the person who asked. Everyone gets the
    designer; only saving needs the Level Pass."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        draft = await build_draft(interaction)
        file = await render_preview(interaction.user, draft)
        await interaction.followup.send(embed=build_embed(draft), file=file,
                                        view=CardWizardView(interaction.user.id, draft), ephemeral=True)
    except Exception:
        logger.exception("[level-card] couldn't open the designer")
        await interaction.followup.send("The card designer is temporarily unavailable.", ephemeral=True)


def trial_embed() -> discord.Embed:
    return discord.Embed(
        title=f"🎁 Free {app_config.LEVEL_CARD_TRIAL_DAYS}-day trial",
        description=(
            f"Try your own level-up card **free for {app_config.LEVEL_CARD_TRIAL_DAYS} days**, no payment: colours, font, "
            "shape, bio, your own background and logo. Your design is saved right now and shows on level-ups, /rank and "
            "My Rank.\n\nOne trial per member. When it ends your design is kept, and comes back with the "
            f"**Level Pass** (${app_config.LEVEL_PASS_FEE_USD:g}, one payment)."),
        color=discord.Color.green())


class TrialView(discord.ui.View):
    """Shown by Save when the member has never had the card plan. Starting the trial saves the draft straight away."""

    def __init__(self, draft: Draft, target: discord.Interaction):
        super().__init__(timeout=300)
        self.draft, self.target = draft, target
        go = discord.ui.Button(label=f"Start free {app_config.LEVEL_CARD_TRIAL_DAYS}-day trial", emoji="🎁",
                               style=discord.ButtonStyle.success)
        go.callback = self._start
        self.add_item(go)
        buy = discord.ui.Button(label=f"Get the Level Pass — ${app_config.LEVEL_PASS_FEE_USD:g}", emoji="🎫",
                                style=discord.ButtonStyle.secondary)
        buy.callback = self._buy
        self.add_item(buy)

    async def _start(self, interaction: discord.Interaction):
        try:
            started = await db.entitlement_start_trial(str(interaction.user.id), app_config.LEVEL_CARD_TRIAL_DAYS)
        except Exception:
            logger.exception("[level-card] couldn't start the trial")
            await interaction.response.send_message("Couldn't start the trial. Try again.", ephemeral=True)
            return
        if not started:
            await interaction.response.edit_message(content="You've already used your free trial.", embed=None, view=None)
            await _offer_pass(interaction, self.draft, self.target, allow_trial=False)
            return
        logger.info("[level-card] free trial started for user %s", interaction.user.id)
        await interaction.response.edit_message(
            content=f"🎁 Your free {app_config.LEVEL_CARD_TRIAL_DAYS}-day trial has started. Saving your card…",
            embed=None, view=None)
        try:
            await save_draft(interaction, self.draft, target=self.target)
        except Exception:
            logger.exception("[level-card] saved nothing after starting the trial")

    async def _buy(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_leveling_boost import _start_boost_checkout
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _start_boost_checkout(interaction, "level_pass", interaction.guild_id)


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


async def _offer_pass(interaction: discord.Interaction, draft: Optional[Draft] = None,
                      target: Optional[discord.Interaction] = None, allow_trial: bool = True) -> None:
    """Save was pressed without a Level Pass: offer the free trial when the member has never had the plan, otherwise
    the pass (private). The draft stays exactly as it is."""
    if allow_trial and draft is not None:
        trial_ok, _on = await trial_state(interaction.user.id)
        if trial_ok:
            draft.trial_available = True
            await interaction.followup.send(embed=trial_embed(), view=TrialView(draft, target or interaction), ephemeral=True)
            return
    from discord_bot.cogs._views_leveling_boost import pass_offered
    if await pass_offered(getattr(interaction.client, "clone_id", None)):
        await interaction.followup.send(embed=pitch_embed(), view=PitchView(interaction.guild_id), ephemeral=True)
    else:
        await interaction.followup.send("🎫 The Level Pass isn't available yet. Your design is still here, try again "
                                        "in a little while.", ephemeral=True)


# ── the designer ─────────────────────────────────────────────────────────────
async def rerender(interaction: discord.Interaction, draft: Draft, notice: str = "") -> None:
    """Redraw the designer message from the draft. The interaction must already be deferred."""
    file = await render_preview(interaction.user, draft)
    await interaction.edit_original_response(embed=build_embed(draft, notice), attachments=[file],
                                             view=CardWizardView(interaction.user.id, draft))


async def edit_draft(interaction: discord.Interaction, draft: Draft, **changes) -> None:
    """Change the draft (nothing is stored) and redraw. Never raises."""
    try:
        await interaction.response.defer()
        new, err = lcd.validate({**draft.design, **changes})
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        draft.design = new
        draft.dirty = True
        await rerender(interaction, draft)
    except Exception:
        logger.exception("[level-card] couldn't update the preview")
        try:
            await interaction.followup.send("Couldn't update the preview. Try again.", ephemeral=True)
        except Exception:
            pass


async def save_draft(interaction: discord.Interaction, draft: Draft, target: Optional[discord.Interaction] = None) -> None:
    """Store the draft. The Level Pass (or the free trial) is re-checked on the server right here. Never raises.
    `target` is the interaction whose original response IS the designer message: set when the save is continued from
    the trial button (the Save press already deferred it), so the designer is redrawn instead of the trial message."""
    try:
        if target is None:
            await interaction.response.defer()
            target = interaction
        if draft.scope == "server":
            # The server card: Manage Server in a Premium server, re-checked right here (never from the buttons).
            if not await can_edit_server(interaction):
                await interaction.followup.send(
                    "Saving the server card needs **Manage Server**, in a server that has Premium.", ephemeral=True)
                return
            uid = draft.key
        else:
            clone = getattr(interaction.client, "clone_id", None)
            has_pass, _stored, _assets, expires = await load_state(interaction.user.id, interaction.guild_id, clone)
            if not has_pass:
                draft.has_pass = False
                await _offer_pass(interaction, draft, target)
                return
            draft.has_pass, draft.expires = True, expires
            uid = str(interaction.user.id)
        design = dict(draft.design)
        problems = []
        for kind, flag in (("background", "custom_bg"), ("logo", "logo")):
            if kind in draft.staged:
                res = await card_assets.submit(db, uid, kind, draft.staged[kind][0])
                if not res["ok"]:
                    design[flag] = False
                    problems.append(res["message"])
                elif res["status"] == "rejected":
                    design[flag] = False
                    problems.append(f"That {kind} can't be used: {res['reason']}")
                else:
                    design[flag] = True
                    if res["status"] == "pending":
                        problems.append("⏳ Your image shows once it passes the automatic check.")
                    else:
                        if draft.scope == "server":
                            await _send_to_host(interaction, kind, uid)
                        else:
                            await _send_to_host(interaction, kind)
            elif not design.get(flag):
                await db.card_asset_delete(uid, kind)                 # the member removed it in the designer
        new, err = lcd.validate(design)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await db.user_card_set(uid, json.dumps(new, separators=(",", ":")))
        fresh = await build_draft(interaction, draft.scope)
        fresh.use_real, fresh.real = draft.use_real, draft.real
        saved_note = ("✅ Saved for the whole server. Members without a card of their own see it."
                      if draft.scope == "server" else "✅ Saved. It shows on your next level-up, on /rank and on My Rank.")
        await rerender(target, fresh, saved_note
                       + ("\n" + "\n".join(problems) if problems else ""))
    except Exception:
        logger.exception("[level-card] couldn't save the design")
        try:
            await interaction.followup.send("Couldn't save that. Try again.", ephemeral=True)
        except Exception:
            pass


class _Pick(discord.ui.Select):
    def __init__(self, draft: Draft, field_name: str, placeholder: str, labels: dict, row: int):
        super().__init__(placeholder=placeholder, row=row, options=[
            discord.SelectOption(label=label, value=key, default=(key == draft.design.get(field_name)))
            for key, label in labels.items()])
        self.draft, self.field_name = draft, field_name

    async def callback(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, **{self.field_name: self.values[0]})


def random_design(rng=random) -> dict:
    """A fresh look from the built-in options (no uploads). Always valid."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(rng.random(), rng.uniform(0.55, 0.85), rng.uniform(0.85, 1.0))
    return {"background": rng.choice(list(lcd.BACKGROUNDS)), "font": rng.choice(list(lcd.FONTS)),
            "shape": rng.choice(list(lcd.SHAPES)), "accent": "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))}


SHAPE_SHORT = {"circle": "Circle", "rounded": "Rounded", "square": "Square"}


def _next_key(options, current):
    keys = list(options)
    return keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]


class CardWizardView(discord.ui.View):
    """Rows: 0 background select · 1 Look (incl. avatar position + fade) · 2 Images and text · 3 Preview · 4 Save (alone)."""

    def __init__(self, owner_id: int, draft: Draft):
        super().__init__(timeout=900)
        self.owner_id, self.draft = owner_id, draft
        d = draft.design
        self.add_item(_Pick(draft, "background", "Background", {k: v[2] for k, v in lcd.BACKGROUNDS.items()}, 0))

        def add(label, emoji, cb, row, disabled=False):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=row, disabled=disabled)
            b.callback = cb
            self.add_item(b)

        # Look
        add("Accent colour", "🎨", self._accent, 1)
        add(f"Font: {lcd.FONT_LABELS.get(d.get('font'), '')}", "🔤", self._cycle_font, 1)
        add(f"Shape: {SHAPE_SHORT.get(d.get('shape'), '')}", "🔷", self._cycle_shape, 1)
        add(f"Avatar: {lcd.AVATAR_POSITIONS.get(d.get('avatar_pos'), '')}", "🧑", self._cycle_avatar_pos, 1)
        add(f"Fade: {lcd.AVATAR_FADES.get(d.get('avatar_fade'), ('',))[0]}", "🌫️", self._cycle_avatar_fade, 1)
        # Images and text
        add("Background image", "🖼️", self._bg, 2)
        add("Logo", "🏷️", self._logo, 2)
        add("Bio", "✏️", self._bio, 2)
        add("Show on card", "👁️", self._show_menu, 2)
        add("Remove uploads", "🗑️", self._remove, 2)
        # Preview
        add("My real level" if not draft.use_real else "Sample level", "📈", self._toggle_real, 3,
            disabled=draft.guild_id is None)
        add("Randomize", "🎲", self._randomize, 3)
        add("Reset", "↩️", self._reset, 3)
        add("Share", "📣", self._share, 3)
        # Save, alone
        if draft.has_pass:
            label, emoji = "Save", "💾"
        elif draft.trial_available:
            label, emoji = "Save (free trial)", "🎁"
        else:
            label, emoji = "Save (Level Pass)", "🔒"
        save = discord.ui.Button(label=label, emoji=emoji,
                                 style=discord.ButtonStyle.success, row=4)
        save.callback = self._save
        self.add_item(save)
        if draft.can_server:                         # Manage Server in a Premium server
            to_server = draft.scope != "server"
            sw = discord.ui.Button(label="Server card" if to_server else "My card", emoji="🏰" if to_server else "🎨",
                                   style=discord.ButtonStyle.primary, row=4)
            sw.callback = self._switch_scope
            self.add_item(sw)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's card designer. Use /levelcard.", ephemeral=True)
            return False
        return True

    async def _accent(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AccentModal(self.draft))

    async def _bio(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BioModal(self.draft))

    async def _cycle_font(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, font=_next_key(lcd.FONTS, self.draft.design.get("font")))

    async def _cycle_shape(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, shape=_next_key(lcd.SHAPES, self.draft.design.get("shape")))

    async def _cycle_avatar_pos(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, avatar_pos=_next_key(lcd.AVATAR_POSITIONS, self.draft.design.get("avatar_pos")))

    async def _cycle_avatar_fade(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, avatar_fade=_next_key(lcd.AVATAR_FADES, self.draft.design.get("avatar_fade")))

    async def _show_menu(self, interaction: discord.Interaction):
        """Swap this message's controls for the multi-select (a select needs a whole row, so it gets its own view)."""
        await interaction.response.edit_message(view=ShowOnCardView(self.owner_id, self.draft))

    async def _bg(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UploadModal("background", self.draft))

    async def _logo(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UploadModal("logo", self.draft))

    async def _remove(self, interaction: discord.Interaction):
        self.draft.staged.clear()
        await edit_draft(interaction, self.draft, custom_bg=False, logo=False)

    async def _save(self, interaction: discord.Interaction):
        await save_draft(interaction, self.draft)

    async def _switch_scope(self, interaction: discord.Interaction):
        """Swap between the member's own card and the server's card. Unsaved changes of the one left are dropped."""
        try:
            await interaction.response.defer()
            want = "user" if self.draft.scope == "server" else "server"
            new = await build_draft(interaction, want)
            note = "Unsaved changes on the other card were dropped." if self.draft.dirty else ""
            if want == "server" and new.scope != "server":
                note = "Saving the server card needs **Manage Server**, in a server that has Premium."
            await rerender(interaction, new, note)
        except Exception:
            logger.exception("[level-card] couldn't switch cards")
            await interaction.followup.send("Couldn't switch right now.", ephemeral=True)

    async def _randomize(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, **random_design())

    async def _reset(self, interaction: discord.Interaction):
        self.draft.staged.clear()
        self.draft.design = dict(lcd.DEFAULT_DESIGN)
        await edit_draft(interaction, self.draft)

    async def _toggle_real(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            d = self.draft
            if not d.use_real and d.real is None and d.guild_id is not None:
                row = await db.get_xp(d.guild_id, interaction.user.id, clone_id=getattr(interaction.client, "clone_id", None))
                from modules import leveling
                p = leveling.xp_progress(int((row or {}).get("total_xp") or 0))
                d.real = (p["level"], p["current_xp_in_level"], p["xp_needed_for_next_level"])
            d.use_real = not d.use_real
            await rerender(interaction, d)
        except Exception:
            logger.exception("[level-card] couldn't load the real level")
            await interaction.followup.send("Couldn't load your level right now.", ephemeral=True)

    async def _share(self, interaction: discord.Interaction):
        """Post the current preview (saved or not) in the channel, so others can see it."""
        try:
            await interaction.response.defer()
            if interaction.channel is None or interaction.guild_id is None:
                await interaction.followup.send("Sharing works inside a server channel.", ephemeral=True)
                return
            d = self.draft
            lvl, cur, need = d.stats()
            png = await lcd.render_async(await _avatar_bytes(interaction.user, d.design.get("accent", "#57F287")),
                                         getattr(interaction.user, "display_name", "You"), lvl, cur, need, d.design,
                                         d.asset("background"), d.asset("logo"))          # full size + 10 MB fallback
            file = discord.File(io.BytesIO(png), filename=lcd.file_name("card", png))
            who = (f"🏰 Server level card, designed by {interaction.user.mention}" if d.scope == "server"
                   else f"🎨 {interaction.user.mention}'s level card")
            await interaction.followup.send(content=who, file=file, ephemeral=False,
                                            allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[level-card] couldn't share the card")
            try:
                await interaction.followup.send("Couldn't share that right now.", ephemeral=True)
            except Exception:
                pass


class ShowOnCardView(discord.ui.View):
    """The "Show on card" menu: one multi-select of the parts of the card that can be hidden. The rest re-flows, so there are no
    gaps. The avatar and the logo are always shown."""

    def __init__(self, owner_id: int, draft: Draft):
        super().__init__(timeout=900)
        self.owner_id, self.draft = owner_id, draft
        sel = discord.ui.Select(
            placeholder="Show on card", min_values=0, max_values=len(lcd.SHOW_KEYS), row=0,
            options=[discord.SelectOption(label=label, value=key, default=bool(draft.design.get(key, True)))
                     for key, label in lcd.SHOW_LABELS.items()])
        sel.callback = self._changed
        self.add_item(sel)
        done = discord.ui.Button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=1)
        done.callback = self._done
        self.add_item(done)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's card designer. Use /levelcard.", ephemeral=True)
            return False
        return True

    async def _changed(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            chosen = set(interaction.data.get("values") or [])
            new, err = lcd.validate({**self.draft.design, **{k: (k in chosen) for k in lcd.SHOW_KEYS}})
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            self.draft.design, self.draft.dirty = new, True
            file = await render_preview(interaction.user, self.draft)
            await interaction.edit_original_response(embed=build_embed(self.draft), attachments=[file],
                                                     view=ShowOnCardView(self.owner_id, self.draft))
        except Exception:
            logger.exception("[level-card] couldn't update what the card shows")
            try:
                await interaction.followup.send("Couldn't update the preview. Try again.", ephemeral=True)
            except Exception:
                pass

    async def _done(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=CardWizardView(self.owner_id, self.draft))


class AccentModal(discord.ui.Modal, title="Accent colour"):
    colour = discord.ui.TextInput(label="Hex colour, like #57F287", min_length=6, max_length=7)

    def __init__(self, draft: Draft):
        super().__init__()
        self.draft = draft
        self.colour.default = draft.design.get("accent", "#57F287")

    async def on_submit(self, interaction: discord.Interaction):
        v = (self.colour.value or "").strip()
        v = v if v.startswith("#") else "#" + v
        if not ACCENT_RE.match(v):
            await interaction.response.send_message("Use a hex colour like #57F287.", ephemeral=True)
            return
        await edit_draft(interaction, self.draft, accent=v)


class BioModal(discord.ui.Modal, title="Card bio"):
    bio = discord.ui.TextInput(label="Short bio (plain text)", required=False, max_length=lcd.BIO_MAX)

    def __init__(self, draft: Draft):
        super().__init__()
        self.draft = draft
        self.bio.default = draft.design.get("bio") or None

    async def on_submit(self, interaction: discord.Interaction):
        await edit_draft(interaction, self.draft, bio=self.bio.value or "")


class UploadModal(discord.ui.Modal):
    """A real file picker (Discord's file-upload modal component). PNG or JPEG, up to the same size as the welcome
    card backgrounds. The image is only checked and previewed here; it is stored and moderated when Save is pressed."""

    def __init__(self, kind: str, draft: Draft):
        super().__init__(title="Background image" if kind == "background" else "Logo")
        self.kind, self.draft = kind, draft
        self.file = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        mb = card_assets.MAX_BYTES[kind] // (1024 * 1024)
        hint = (f"Up to {mb} MB, any shape. The card takes your image's size. Keep the middle calm, text goes there."
                if kind == "background" else f"Up to {mb} MB. Shown on the right side of the card.")
        self.add_item(discord.ui.Label(text="PNG or JPEG", description=hint, component=self.file))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            att = self.file.values[0]
            cap = card_assets.MAX_BYTES[self.kind]
            if (att.content_type or "").split(";")[0] not in ACCEPTED:
                await interaction.response.send_message("Only PNG or JPEG images, please.", ephemeral=True)
                return
            if att.size > cap:
                await interaction.response.send_message(f"That file is too big (max {cap // (1024 * 1024)} MB).", ephemeral=True)
                return
            await interaction.response.defer()
            raw = await att.read()
            png, err = await asyncio.to_thread(card_assets.process, self.kind, raw)
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            self.draft.staged[self.kind] = (raw, png)
            flag = "custom_bg" if self.kind == "background" else "logo"
            new, _ = lcd.validate({**self.draft.design, flag: True})
            self.draft.design = new
            self.draft.dirty = True
            await rerender(interaction, self.draft)
        except Exception:
            logger.exception("[level-card] upload failed")
            try:
                await interaction.followup.send("Couldn't use that image. Try again.", ephemeral=True)
            except Exception:
                pass


async def _send_to_host(interaction: discord.Interaction, kind: str, key: Optional[str] = None) -> None:
    """Send the processed, APPROVED image to the same hosting channel the welcome backgrounds use. Best effort."""
    try:
        from modules import image_host
        key = key or str(interaction.user.id)
        row = await db.card_asset_get(key, kind)
        if row and row.get("status") == "approved":
            who = f"server {key[2:]}" if key.startswith("g:") else f"user {key}"
            await image_host.post_to_host(interaction.client, bytes(row["data"]), f"Level card {kind} · {who} · approved")
    except Exception:
        logger.warning("[level-card] couldn't copy the image to the hosting channel", exc_info=True)
