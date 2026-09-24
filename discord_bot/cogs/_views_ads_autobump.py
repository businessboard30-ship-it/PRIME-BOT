# path: discord_bot/cogs/_views_ads_autobump.py

"""
Owner wizard for the ad AUTO-BUMP (/ad autobump) — the job that re-posts every
approved network ad into every bump channel (api/cron_ad_placement.py).

Change it any time, no ids, no env vars:
  • Turn auto-bump on / off (off = the cron posts nothing, ads stay approved)
  • How often each ad repeats per bump channel (15 min – 7 days)
  • Run a placement pass right now (still respects the timer)

Settings live in admin_config (modules/ads_marketplace.py), so the serverless
cron and this wizard always agree. Same DynamicItem / restart-proof pattern as
_views_ads_wizard.py, with the owner gate re-checked on every click.
"""

import logging
import re
from datetime import timezone

import discord

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from modules.ads_marketplace import (
    get_active_ads, get_autobump_settings, get_autobump_stats,
    set_autobump_enabled, set_autobump_interval, format_interval,
    AUTOBUMP_MIN_INTERVAL, AUTOBUMP_MAX_INTERVAL,
)

logger = logging.getLogger(__name__)

# (label, seconds) — quick picks for the dropdown; "Custom" covers everything else.
_PRESETS = [
    ("Every 15 minutes", 15 * 60),
    ("Every 30 minutes", 30 * 60),
    ("Every hour", 60 * 60),
    ("Every 2 hours", 2 * 60 * 60),
    ("Every 3 hours", 3 * 60 * 60),
    ("Every 6 hours (default)", 6 * 60 * 60),
    ("Every 12 hours", 12 * 60 * 60),
    ("Every 24 hours", 24 * 60 * 60),
]


def _is_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _unix(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


async def _bump_channel_count() -> int:
    try:
        return len(await db.get_all_bump_channels())
    except Exception:
        logger.exception("[ads-autobump] couldn't count bump channels")
        return 0


async def build_autobump_view(note: str = None) -> discord.ui.LayoutView:
    settings = await get_autobump_settings()
    stats = await get_autobump_stats()
    live_ads = len(await get_active_ads())
    channels = await _bump_channel_count()

    enabled = settings["enabled"]
    interval = settings["interval_seconds"]

    lines = [
        "### 🔁 Ad auto-bump",
        f"**Status:** {'🟢 ON' if enabled else '⏸️ OFF — no ads are being re-posted'}",
        f"**Repeats every:** {format_interval(interval)} (per ad, per bump channel)",
        f"**Live ads:** {live_ads}  ·  **Bump channels:** {channels}",
    ]
    if stats["last_posted_at"]:
        lines.append(
            f"**Last placed:** <t:{_unix(stats['last_posted_at'])}:R>  ·  **Placed in last 24h:** {stats['posted_24h']}"
        )
    else:
        lines.append(
            "**Last placed:** never — nothing has triggered the placement job yet. "
            "Schedule the cron (api/cron_ad_placement.py) or tap **Run now**."
        )
    if note:
        lines += ["", note]

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.teal())
    container.add_item(discord.ui.TextDisplay("\n".join(lines)))
    container.add_item(discord.ui.Separator())

    pick_row = discord.ui.ActionRow()
    pick_row.add_item(AutobumpIntervalSelect(interval))
    container.add_item(pick_row)

    btn_row = discord.ui.ActionRow()
    btn_row.add_item(AutobumpButton("toggle", enabled))
    btn_row.add_item(AutobumpButton("custom", enabled))
    btn_row.add_item(AutobumpButton("run", enabled))
    container.add_item(btn_row)

    view.add_item(container)
    return view


async def render(interaction: discord.Interaction, note: str = None):
    view = await build_autobump_view(note)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


async def _deny(interaction: discord.Interaction):
    msg = "You're not authorized to manage ads."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


class AutobumpIntervalSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"adab_interval"):
    def __init__(self, current_seconds: int = None):
        options = [
            discord.SelectOption(label=label, value=str(secs), default=(secs == current_seconds))
            for label, secs in _PRESETS
        ]
        if current_seconds is not None and all(secs != current_seconds for _, secs in _PRESETS):
            options.insert(0, discord.SelectOption(
                label=f"Every {format_interval(current_seconds)} (custom)", value=str(current_seconds), default=True,
            ))
        super().__init__(discord.ui.Select(
            placeholder="How often should each ad repeat?", options=options[:25], custom_id="adab_interval",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        saved = await set_autobump_interval(int(self.item.values[0]))
        if saved is None:
            await render(interaction, "❌ Couldn't save that — try again.")
            return
        await render(interaction, f"✅ Ads will now repeat every **{format_interval(saved)}**.")


_BUTTONS = {
    "custom": ("✏️ Custom time", discord.ButtonStyle.secondary),
    "run": ("▶️ Run now", discord.ButtonStyle.primary),
}


class AutobumpButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"adab_(?P<act>toggle|custom|run):(?P<on>[01])",
):
    def __init__(self, act: str, enabled: bool):
        self.act = act
        self.enabled = enabled
        if act == "toggle":
            label, style = ("⏸️ Turn off", discord.ButtonStyle.danger) if enabled else ("▶️ Turn on", discord.ButtonStyle.success)
        else:
            label, style = _BUTTONS[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"adab_{act}:{int(enabled)}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match.group("act"), match.group("on") == "1")

    async def callback(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return

        if self.act == "custom":
            await interaction.response.send_modal(AutobumpCustomModal())
            return

        if self.act == "toggle":
            # Flip the LIVE value, not the one baked into this button's custom_id —
            # a stale message must never silently do the opposite of what it shows.
            current = (await get_autobump_settings())["enabled"]
            ok = await set_autobump_enabled(not current)
            if not ok:
                await render(interaction, "❌ Couldn't save that — try again.")
                return
            await render(interaction, "🟢 Auto-bump is **ON**." if not current else "⏸️ Auto-bump is **OFF** — ads stay approved but nothing is re-posted.")
            return

        # act == "run"
        await interaction.response.defer()
        try:
            from api.cron_ad_placement import run_ad_placements
            result = await run_ad_placements()
        except Exception:
            logger.exception("[ads-autobump] manual run failed")
            await render(interaction, "❌ The placement run crashed — check the logs.")
            return
        if result.get("skipped"):
            await render(interaction, f"⏸️ Nothing posted — {result['skipped']}. Turn it on first.")
            return
        await render(
            interaction,
            f"▶️ Ran a placement pass: **{result['placed']}** posted, **{result['failed']}** failed "
            f"({result['ads']} live ad(s), {result['channels']} bump channel(s)). "
            "Ads that were posted more recently than the timer are skipped on purpose.",
        )


class AutobumpCustomModal(discord.ui.Modal, title="Custom repeat time"):
    def __init__(self):
        super().__init__()
        self.minutes = discord.ui.TextInput(placeholder="e.g. 90", max_length=6)
        self.add_item(discord.ui.Label(
            text="Repeat every … minutes",
            description=f"{AUTOBUMP_MIN_INTERVAL // 60} to {AUTOBUMP_MAX_INTERVAL // 60} minutes",
            component=self.minutes,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer()
        raw = str(self.minutes.value).strip()
        try:
            minutes = float(raw)
        except ValueError:
            await interaction.followup.send("Enter a number of minutes, like 90.", ephemeral=True)
            return
        seconds = int(minutes * 60)
        if seconds < AUTOBUMP_MIN_INTERVAL or seconds > AUTOBUMP_MAX_INTERVAL:
            await interaction.followup.send(
                f"Pick between {AUTOBUMP_MIN_INTERVAL // 60} and {AUTOBUMP_MAX_INTERVAL // 60} minutes.", ephemeral=True,
            )
            return
        saved = await set_autobump_interval(seconds)
        if saved is None:
            await render(interaction, "❌ Couldn't save that — try again.")
            return
        await render(interaction, f"✅ Ads will now repeat every **{format_interval(saved)}**.")


DYNAMIC_ITEMS = (AutobumpIntervalSelect, AutobumpButton)
