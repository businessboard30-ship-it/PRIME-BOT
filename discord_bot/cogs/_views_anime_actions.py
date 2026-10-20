# path: discord_bot/cogs/_views_anime_actions.py

"""
Anime feed buttons that go on the posted card and open private (ephemeral) panels: Alert me (D2). Every item is a DynamicItem whose
custom_id carries all the state it needs (the AniList id, the chosen value), so each one keeps working on any old card or panel
after a restart or deploy. Anyone can use them; replies are ephemeral. Admin-only settings stay in the setup wizard.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord

from modules import anime_feed as af
from modules import anime_feed_alerts as alerts
from modules import anime_plus as plus
from discord_bot.cogs._views_anime_feed import ACCENT, OOPS, _title_from_message, _walk

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
MODE_TEXT = {"mention": "Mention me in the server", "dm": "DM me", "off": "Mute this anime"}
MODE_EMOJI = {"mention": "🔔", "dm": "📩", "off": "🔕"}
PANEL_COOLDOWN = 2.0
LOCKED_DM = "🔒 DM alerts are part of the upgrade. Tap **Upgrade** on the card to unlock them (premium servers have them for everyone)."
LOCKED_QUIET = "🔒 Quiet hours are part of the upgrade. Tap **Upgrade** on the card to unlock them (premium servers have them for everyone)."


async def _plus_state(interaction: discord.Interaction) -> bool:
    return await plus.has_anime_plus(interaction.user.id, interaction.guild.id if interaction.guild else None, getattr(interaction.client, "clone_id", None) or 0)


async def _answer_error(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(OOPS, ephemeral=True)
        else:
            await interaction.response.send_message(OOPS, ephemeral=True)
    except discord.HTTPException:
        pass


def _panel_title(interaction: discord.Interaction) -> Optional[str]:
    """The anime title is in the panel's own heading ('## 🔔 Alerts: Title'); nothing extra is stored anywhere."""
    try:
        for c in _walk(interaction.message.components):
            m = re.match(r"^## 🔔 Alerts: (.+)", getattr(c, "content", "") or "")
            if m:
                return m.group(1).strip()[:120]
    except Exception:
        pass
    return None


def panel_text(title: str, mode: Optional[str], prefs: alerts.Prefs, note: Optional[str] = None) -> str:
    lines = [note, ""] if note else []
    lines += [f"## 🔔 Alerts: {title}"[:200],
              "How should I tell you when a new episode comes out?" if mode else "You don't follow this anime yet. Pick how you'd like to hear about it and I'll follow it for you.",
              f"**Now:** {MODE_EMOJI[mode]} {MODE_TEXT[mode]}" if mode else "**Now:** not following",
              "", "**Quiet hours** (account-wide): I won't DM you or mention you in these hours, in your timezone. Pick your timezone first.",
              "-# DM alerts are opt-in: nobody is messaged unless they choose DM here. If your DMs are closed I switch you back to mentions."]
    return "\n".join(lines)


class _AlertButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_alert:(\d+)$"):
    """Alert me: opens a private panel to choose mention / DM / mute for this anime and set quiet hours. Anyone can use it."""

    def __init__(self, anime_id: int):
        self.anime_id = int(anime_id)
        super().__init__(discord.ui.Button(label="Alert me", style=discord.ButtonStyle.secondary, emoji="⏰", custom_id=f"anime_alert:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        wait = af.cooldown_left(interaction.user.id, "alert", PANEL_COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)          # answer inside Discord's 3 seconds first
            title = _title_from_message(interaction) or f"Anime {self.anime_id}"
            mode = await alerts.get_mode(interaction.user.id, self.anime_id)
            prefs = await alerts.get_prefs(interaction.user.id)
            note = None
            if prefs.fell_back:
                note = "ℹ️ I couldn't DM you (your DMs are closed to me), so I switched your DM alerts back to mentions. Open your DMs and choose DM again if you want them."
                await alerts.clear_fell_back(interaction.user.id)
            await interaction.followup.send(view=alert_panel(self.anime_id, title, mode, prefs, note), ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] alert panel failed")
            await _answer_error(interaction)


class _ModeSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^anime_amode:(\d+)$"):
    def __init__(self, anime_id: int, mode: Optional[str] = None):
        self.anime_id = int(anime_id)
        opts = [discord.SelectOption(label=MODE_TEXT[m], value=m, emoji=MODE_EMOJI[m], default=(m == mode)) for m in alerts.MODES]
        super().__init__(discord.ui.Select(placeholder="How should I alert you?", options=opts, min_values=1, max_values=1,
                                           custom_id=f"anime_amode:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        mode = (interaction.data.get("values") or [""])[0]
        if mode not in alerts.MODES:
            await interaction.response.send_message("🚫 That choice isn't available.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
            title = _panel_title(interaction) or f"Anime {self.anime_id}"
            has_plus = await _plus_state(interaction)
            if mode == "dm" and not has_plus:                          # DM alerts are an exclusive feature: nothing is changed
                current = await alerts.get_mode(interaction.user.id, self.anime_id)
                view = alert_panel(self.anime_id, title, current, await alerts.get_prefs(interaction.user.id), LOCKED_DM)
                await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
                return
            state, n = await alerts.set_mode(interaction.user.id, self.anime_id, title, mode, plus.PLUS_FOLLOW_LIMIT if has_plus else plus.FREE_FOLLOW_LIMIT)
            if state == "limit":
                current = await alerts.get_mode(interaction.user.id, self.anime_id)
                note = f"🚫 You already follow {n} anime, your limit. Unfollow one first (tap its Follow button again)." + ("" if has_plus else " Tap **Upgrade** to follow up to 50.")
                view = alert_panel(self.anime_id, title, current, await alerts.get_prefs(interaction.user.id), note)
            else:
                view = alert_panel(self.anime_id, title, mode, await alerts.get_prefs(interaction.user.id), f"✅ {MODE_EMOJI[mode]} {MODE_TEXT[mode]}.")
            await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] alert mode failed")
            await _answer_error(interaction)


class _TzSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^anime_atz:(\d+)$"):
    def __init__(self, anime_id: int, tz: str = "UTC+0"):
        self.anime_id = int(anime_id)
        opts = [discord.SelectOption(label=alerts.tz_label(h), value=str(h), default=(alerts.tz_label(h) == tz)) for h in alerts.TZ_CHOICES]
        super().__init__(discord.ui.Select(placeholder="Your timezone", options=opts, min_values=1, max_values=1, custom_id=f"anime_atz:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        try:
            hours = int((interaction.data.get("values") or ["x"])[0])
            if hours not in alerts.TZ_CHOICES:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("🚫 That choice isn't available.", ephemeral=True)
            return
        await _save_pref(interaction, self.anime_id, lambda uid: alerts.set_tz(uid, hours), f"✅ Timezone set to {alerts.tz_label(hours)}.")


class _QuietSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^anime_aq:(\d+)$"):
    def __init__(self, anime_id: int, key: str = "off"):
        self.anime_id = int(anime_id)
        opts = [discord.SelectOption(label="No quiet hours" if k == "off" else f"Quiet {k.split('-')[0]}:00 to {k.split('-')[1]}:00", value=k, default=(k == key))
                for k in alerts.QUIET_PRESETS]
        super().__init__(discord.ui.Select(placeholder="Quiet hours", options=opts, min_values=1, max_values=1, custom_id=f"anime_aq:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        key = (interaction.data.get("values") or [""])[0]
        if key not in alerts.QUIET_PRESETS:
            await interaction.response.send_message("🚫 That choice isn't available.", ephemeral=True)
            return
        if key != "off" and not await _plus_state(interaction):          # quiet hours are an exclusive feature; turning them OFF is always allowed
            await _save_pref(interaction, self.anime_id, None, LOCKED_QUIET)
            return
        await _save_pref(interaction, self.anime_id, lambda uid: alerts.set_quiet(uid, key), "✅ Quiet hours off." if key == "off" else "✅ Quiet hours saved.")


async def _save_pref(interaction: discord.Interaction, anime_id: int, save, note: str) -> None:
    try:
        await interaction.response.defer()
        if save is not None:
            await save(interaction.user.id)
        title = _panel_title(interaction) or f"Anime {anime_id}"
        view = alert_panel(anime_id, title, await alerts.get_mode(interaction.user.id, anime_id), await alerts.get_prefs(interaction.user.id), note)
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
    except Exception:
        logger.exception("[anime-feed] alert preference failed")
        await _answer_error(interaction)


class _MuteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_mute:(\d+)$"):
    """On every DM alert: one tap to stop alerts for this anime (it stays followed)."""

    def __init__(self, anime_id: int):
        self.anime_id = int(anime_id)
        super().__init__(discord.ui.Button(label="Mute", style=discord.ButtonStyle.secondary, emoji="🔕", custom_id=f"anime_mute:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        try:
            title = _title_from_message(interaction) or f"Anime {self.anime_id}"
            state, _ = await alerts.set_mode(interaction.user.id, self.anime_id, title, "off", af.FREE_FOLLOW_LIMIT)
            text = f"🔕 Muted **{title}**. It stays in your follows; tap Alert me to turn alerts back on." if state == "ok" else OOPS
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] mute failed")
            await _answer_error(interaction)


def alert_panel(anime_id: int, title: str, mode: Optional[str], prefs: alerts.Prefs, note: Optional[str] = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(panel_text(title, mode, prefs, note)[:3900]))
    box.add_item(discord.ui.ActionRow(_ModeSelect(anime_id, mode)))
    box.add_item(discord.ui.ActionRow(_TzSelect(anime_id, prefs.tz)))
    box.add_item(discord.ui.ActionRow(_QuietSelect(anime_id, alerts.preset_key(prefs))))
    view.add_item(box)
    return view


ACTION_ITEMS = (_AlertButton, _ModeSelect, _TzSelect, _QuietSelect, _MuteButton)
