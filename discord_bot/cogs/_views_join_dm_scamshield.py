# path: discord_bot/cogs/_views_join_dm_scamshield.py

"""
"Scam Shield" on the combined owner join DM (replaces the old "List your server" button, the directory isn't ready).

The button on page 1 of the join DM (OpenScamShieldButton) swaps THAT SAME MESSAGE into this sub-screen, the same way the
Welcome "Turn on" button swaps into the welcome preview (see _views_join_dm.build_welcome_sub_view):

  * an example scam picture ("SCAM" stamp, "Scam Shield blocked this message"), attached to the edit
  * what happens to whoever posts a scam: Delete only / Timeout / Quarantine / Kick / Auto-ban (free)
  * on/off, Check a link, Allow domains, Recent catches (free)
  * Strict mode, Deep report and Upgrade to Premium (premium upgrades, same gating as /scamshield)
  * Back, which rebuilds page 1 of the join DM

Every control is a DynamicItem with guild_id/clone_id in its custom_id, so it keeps working after a bot restart and
never times out. The scam logic itself lives in modules/scam_shield*.py; the wizard helpers (status text, link checker,
reports, premium pitch) are reused from _views_scamshield_wizard.py so the two screens can't drift apart.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import discord

from modules import antiraid_pro as pro
from modules import scam_shield as ss
from modules import scam_shield_actions as sa
from modules import server_panel_protection as spp
from discord_bot.cogs import _views_scamshield_wizard as wiz
from discord_bot.cogs._views_shared import user_can_manage_guild

logger = logging.getLogger(__name__)

IMAGE_PATH = Path(__file__).resolve().parents[2] / "assets" / "images" / "scam_shield_example.png"
IMAGE_NAME = "scam_shield_example.png"
_NO_PINGS = discord.AllowedMentions.none()

ACTION_KINDS = tuple(f"act_{a}" for a in ss.PUNISH_ACTIONS)
KINDS = ("toggle", "check", "allow", "recent", "strict", "deep", "upgrade", "back") + ACTION_KINDS
_PAT = r"^joindm_scam:(?P<kind>" + "|".join(KINDS) + r"):(?P<guild>\d+):(?P<clone>-|\d+)$"
_OPEN_PAT = r"^join_dm_scam:(?P<guild>\d+):(?P<clone>-|\d+)$"


def _clone_part(clone_id) -> str:
    return "-" if clone_id is None else str(clone_id)


def _clone_from(part: str):
    return None if part == "-" else int(part)


# ── the screen ────────────────────────────────────────────────────────────

def _action_line(current: str) -> str:
    label, emoji, _perm, blurb = sa.ACTIONS.get(current, sa.ACTIONS["delete"])
    return f"Right now: {emoji} **{label}**. {blurb}"


def build_container(guild: discord.Guild, clone_id, st: dict, note: str = "") -> discord.ui.Container:
    cfg = st["scam"].get("cfg", {})
    on = bool(cfg.get("enabled", True))
    premium = st["premium"]
    current = cfg.get("punish_action") if cfg.get("punish_action") in ss.PUNISH_ACTIONS else "delete"

    container = discord.ui.Container(accent_colour=discord.Color.green() if on else discord.Color.orange())
    if note:      # a Components V2 message can't carry plain `content`, so the note lives inside the container
        container.add_item(discord.ui.TextDisplay(note))
    container.add_item(discord.ui.TextDisplay(
        "## 🛡️ Scam Shield\n"
        "Deletes fake giveaways, crypto bait, fake payout screenshots and look-alike links before your members fall "
        "for them.\n" + "\n".join(wiz.status_lines(guild, st))
    ))
    container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(
        media=f"attachment://{IMAGE_NAME}", description="Example of a scam message that Scam Shield blocks")))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        "**When someone posts a scam**\nThe message is always deleted. Pick what also happens to the sender:\n"
        + _action_line(current)))
    actions = discord.ui.ActionRow()
    for action in ss.PUNISH_ACTIONS:
        actions.add_item(ScamSubButton(f"act_{action}", guild.id, clone_id, on=on, current=current))
    container.add_item(actions)
    container.add_item(discord.ui.TextDisplay(
        "-# Staff, bots and the server owner are never punished. Quarantine sets up a hidden role for you in one tap."))
    container.add_item(discord.ui.Separator())

    if premium:
        container.add_item(discord.ui.TextDisplay(
            f"**💎 Premium extras**\nStrict mode: {'🟢 on' if st['strict'] else '⚪ off'} · Deep reports: ✅ unlocked"))
    else:
        container.add_item(discord.ui.TextDisplay(wiz.PITCH))

    free = discord.ui.ActionRow()
    for kind in ("toggle", "check", "allow", "recent"):
        free.add_item(ScamSubButton(kind, guild.id, clone_id, on=on, current=current))
    container.add_item(free)

    extra = discord.ui.ActionRow()
    extra.add_item(ScamSubButton("strict", guild.id, clone_id, premium=premium, strict=st["strict"]))
    extra.add_item(ScamSubButton("deep", guild.id, clone_id, premium=premium))
    if not premium:
        extra.add_item(ScamSubButton("upgrade", guild.id, clone_id))
    extra.add_item(ScamSubButton("back", guild.id, clone_id))
    container.add_item(extra)
    return container


def build_view(container: discord.ui.Container) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def image_file() -> discord.File:
    return discord.File(str(IMAGE_PATH), filename=IMAGE_NAME)


async def render(guild: discord.Guild, clone_id, note: str = "") -> tuple:
    """-> (view, file). Pass both to edit_original_response(view=..., attachments=[file])."""
    st = await wiz.load_state(guild, clone_id)
    return build_view(build_container(guild, clone_id, st, note)), image_file()


async def _rerender(interaction: discord.Interaction, guild: discord.Guild, clone_id, note: str = "") -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    view, file = await render(guild, clone_id, note)
    await interaction.edit_original_response(view=view, attachments=[file])


async def _say(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=_NO_PINGS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=_NO_PINGS)


# ── modal ─────────────────────────────────────────────────────────────────

class _AllowModal(wiz.AllowModal):
    """Same form as the /scamshield one, but afterwards redraws THIS screen (the base class would swap the DM into the
    standalone wizard panel)."""

    async def on_submit(self, interaction: discord.Interaction):
        guild = await wiz._auth(interaction, self.guild_id)
        if guild is None:
            return
        from database import db
        await interaction.response.defer()
        current = (await db.get_scam_shield_guild(guild.id, clone_id=self.clone_id)).get("allowed_domains") or []
        added, rejected, final = spp.clean_domains(str(self.domains.value), current)
        if added:
            await spp.set_allowed_domains(guild.id, self.clone_id, interaction.user.id, final)
        view, file = await render(guild, self.clone_id)
        await interaction.edit_original_response(view=view, attachments=[file])
        note = []
        if added:
            note.append("✅ Allowed: " + ", ".join(f"`{d}`" for d in added))
        if rejected:
            note.append("⚠️ Skipped (not a valid domain, or the 50-domain limit): " + ", ".join(f"`{r}`" for r in rejected))
        if note:
            await interaction.followup.send("\n".join(note), ephemeral=True)


# ── buttons ───────────────────────────────────────────────────────────────

class ScamSubButton(discord.ui.DynamicItem[discord.ui.Button], template=_PAT):
    def __init__(self, kind: str, guild_id: int, clone_id, on: bool = True, premium: bool = True,
                 strict: bool = False, current: str = "delete"):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        style, emoji, disabled = discord.ButtonStyle.secondary, None, False
        if kind.startswith("act_"):
            action = kind[4:]
            label, emoji, _perm, _blurb = sa.ACTIONS[action]
            selected = action == current
            style = discord.ButtonStyle.success if selected else (
                discord.ButtonStyle.danger if action == "ban" else discord.ButtonStyle.secondary)
            disabled = selected
            if selected:
                label = f"{label} ✓"
        else:
            label, style, emoji = {
                "toggle": ("Turn off here" if on else "Turn on here",
                           discord.ButtonStyle.danger if on else discord.ButtonStyle.success, None),
                "check": ("Check a link", discord.ButtonStyle.secondary, "🔎"),
                "allow": ("Allow domains", discord.ButtonStyle.secondary, "➕"),
                "recent": ("Recent catches", discord.ButtonStyle.secondary, "📜"),
                "strict": ((("Strict: on, turn off" if strict else "Strict mode") if premium else "Strict mode 🔒"),
                           (discord.ButtonStyle.danger if strict else discord.ButtonStyle.success) if premium
                           else discord.ButtonStyle.secondary, "💎" if premium else None),
                "deep": ("Deep report" if premium else "Deep report 🔒",
                         discord.ButtonStyle.success if premium else discord.ButtonStyle.secondary,
                         "📊" if premium else None),
                "upgrade": ("Upgrade to Premium", discord.ButtonStyle.primary, "💎"),
                "back": ("Back", discord.ButtonStyle.secondary, "⬅️"),
            }[kind]
        super().__init__(discord.ui.Button(
            label=label, style=style, emoji=emoji, disabled=disabled,
            custom_id=f"joindm_scam:{kind}:{guild_id}:{_clone_part(clone_id)}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("kind"), int(match.group("guild")), _clone_from(match.group("clone")))

    async def callback(self, interaction: discord.Interaction):
        guild = await wiz._auth(interaction, self.guild_id)
        if guild is None:
            return
        try:
            if self.kind.startswith("act_"):
                await self._do_action(interaction, guild, self.kind[4:])
            else:
                await getattr(self, f"_do_{self.kind}")(interaction, guild)
        except Exception:
            logger.exception("[join-dm-scamshield] %s failed in guild %s", self.kind, self.guild_id)
            await _say(interaction, "⚠️ Something went wrong. Please try again.")

    async def _do_action(self, interaction, guild, action: str):
        await interaction.response.defer()
        label = sa.ACTIONS[action][0]
        missing = sa.missing_permission(guild, action)
        if missing:
            await interaction.followup.send(f"⚠️ {missing} Then tap **{label}** again.", ephemeral=True)
            return
        note = ""
        if action == "quarantine":
            ok, msg = await sa.ensure_quarantine_role(guild, self.clone_id, interaction.user.id)
            if not ok:
                await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
                return
            note = msg
        await spp.set_scam_action(guild.id, self.clone_id, interaction.user.id, action)
        text = f"✅ Scam senders will now be: {sa.ACTIONS[action][1]} **{label}**."
        await _rerender(interaction, guild, self.clone_id, note=text + (f"\n{note}" if note else ""))

    async def _do_toggle(self, interaction, guild):
        await interaction.response.defer()
        cfg = (await spp.scam_state(guild.id, self.clone_id))["cfg"]
        await spp.set_scam_enabled(guild.id, self.clone_id, interaction.user.id, not bool(cfg.get("enabled", True)))
        await _rerender(interaction, guild, self.clone_id)

    async def _do_check(self, interaction, guild):
        await interaction.response.send_modal(wiz.CheckModal(guild.id, self.clone_id))

    async def _do_allow(self, interaction, guild):
        await interaction.response.send_modal(_AllowModal(guild.id, self.clone_id))

    async def _do_recent(self, interaction, guild):
        await interaction.response.defer(ephemeral=True)
        rows = await ss.recent_guild_hits(guild.id, self.clone_id, limit=5)
        await interaction.followup.send(wiz.recent_text(rows), ephemeral=True, allowed_mentions=_NO_PINGS)

    async def _do_upgrade(self, interaction, guild):
        if await pro.is_premium(guild.id, self.clone_id):
            await _say(interaction, "💎 This server already has premium. Strict mode and Deep reports are unlocked.")
            return
        await wiz._pitch(interaction, guild, self.clone_id, "Strict Scam Shield and Deep reports")

    async def _do_strict(self, interaction, guild):
        if not await pro.is_premium(guild.id, self.clone_id):
            await wiz._pitch(interaction, guild, self.clone_id, "Strict Scam Shield")
            return
        await interaction.response.defer()
        turn_on = not ss.strict_on(guild.id, self.clone_id)
        await ss.set_strict(guild.id, self.clone_id, turn_on)
        from modules.server_panel import record_change
        await record_change(guild.id, self.clone_id, interaction.user.id, "scam_shield.strict", not turn_on, turn_on)
        await _rerender(interaction, guild, self.clone_id)
        await interaction.followup.send(
            ("🟢 **Strict mode is on.** It also catches look-alike links, fake-subdomain tricks and \"free nitro / "
             "airdrop\" bait. Use **Check a link** to test a link and **Allow domains** for real sites it blocks.")
            if turn_on else "⚪ Strict mode is off. The normal Scam Shield is still protecting this server.",
            ephemeral=True)

    async def _do_deep(self, interaction, guild):
        if not await pro.is_premium(guild.id, self.clone_id):
            await wiz._pitch(interaction, guild, self.clone_id, "Deep reports")
            return
        await interaction.response.defer(ephemeral=True)
        rep = await ss.deep_report(guild.id, self.clone_id)
        await interaction.followup.send(wiz.deep_text(rep), ephemeral=True, allowed_mentions=_NO_PINGS)

    async def _do_back(self, interaction, guild):
        # Defer first: the lookups below can outrun Discord's 3-second window (see _views_join_dm._PageNavButton).
        await interaction.response.defer()
        from discord_bot.cogs import _views_join_dm as jd
        enabled = await jd._enabled_feature_keys(self.guild_id, self.clone_id)
        intro, title, notices = await jd._build_main_join_dm_parts(interaction.client, self.guild_id, self.clone_id)
        main_view = jd.build_join_dm_view(
            self.guild_id, clone_id=self.clone_id, feature_keys=list(jd.FEATURE_TOGGLES.keys()), page=0,
            intro=intro, title=title, notices=notices, enabled_keys=enabled, guild_name=guild.name,
        )
        # attachments=[] clears the example picture left on the message, otherwise Discord keeps serving it.
        await interaction.edit_original_response(view=main_view, attachments=[])


class OpenScamShieldButton(discord.ui.DynamicItem[discord.ui.Button], template=_OPEN_PAT):
    """The button on the join DM (page 1) that replaced "List your server". Needs Manage Server in that server. Works
    from the DM and from the in-server backup copy, because the guild comes from the custom_id."""

    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label="Scam Shield", style=discord.ButtonStyle.primary, emoji="🚨",
            custom_id=f"join_dm_scam:{guild_id}:{_clone_part(clone_id)}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group("guild")), _clone_from(match.group("clone")))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await _say(interaction, "I'm not in that server anymore.")
            return
        if not await user_can_manage_guild(guild, interaction.user.id):
            await _say(interaction, "You need the **Manage Server** permission to set up Scam Shield.")
            return
        # Defer FIRST (3-second window), then swap this same message into the Scam Shield screen.
        await interaction.response.defer()
        try:
            view, file = await render(guild, self.clone_id)
            await interaction.edit_original_response(view=view, attachments=[file])
        except Exception:
            logger.exception("[join-dm-scamshield] couldn't open the screen for guild %s", self.guild_id)
            await interaction.followup.send(
                "⚠️ Couldn't open Scam Shield right now. Try again, or run `/scamshield` in your server.", ephemeral=True)


DYNAMIC_ITEMS = (ScamSubButton, OpenScamShieldButton)
