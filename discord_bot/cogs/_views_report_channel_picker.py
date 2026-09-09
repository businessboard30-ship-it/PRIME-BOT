# path: discord_bot/cogs/_views_report_channel_picker.py

"""
One-time DM asking the bot's admin(s) to pick a channel where public
"report this server" submissions (app/servers/_ServerDirectory.tsx's
Report button -> api/server_listings.py's report mode -> database.py's
report_listing) get forwarded. No slash command needed — the same
ChannelSelect-in-a-DM pattern several setup wizards already use
(_views_automod_wizard.py's AutomodLogChannelSelect is the closest
match), just sent proactively instead of from a guild-side wizard.

Reports are written by api/server_listings.py, a separate serverless
process with no live Discord connection (same "API writes, bot process
polls and acts" split as server_listing.py's voting-panel poller) — so
this module only handles the DM/picker side. The actual polling that
posts pending reports into the configured channel lives in
discord_bot/cogs/report_notifications.py.
"""

import logging
import re

import discord

from database import db

logger = logging.getLogger(__name__)


def _clone_id_of_bot(bot) -> int | None:
    return getattr(bot, "clone_id", None)


def _encode(clone_id) -> str:
    # -1 stands in for "no clone" (NULL) since custom_id has no room for
    # an actual None — decoded back to None in _decode below.
    return f"reportchan:pick:{clone_id if clone_id is not None else -1}"


def _id_pattern() -> str:
    return r"reportchan:pick:(?P<clone_id>-?\d+)"


async def prompt_report_channel(bot) -> None:
    """Called once per clone from bot.py's on_ready (same backfill-safe
    shape as offer_auto_listing) — best-effort, never raises. Skips
    silently if a channel is already configured, or if this clone's
    admin(s) were already asked (claim_report_channel_prompt_send is an
    atomic insert-once, so this is safe to call on every restart)."""
    clone_id = _clone_id_of_bot(bot)
    try:
        existing = await db.get_report_notify_channel(clone_id)
        if existing and (existing.get("channel_id") or existing.get("dm_user_id")):
            return  # already configured
        claimed = await db.claim_report_channel_prompt_send(clone_id)
    except Exception:
        logger.exception("[report-channel-picker] claim/check failed")
        return
    if not claimed:
        return  # already asked, still unanswered — don't re-prompt every restart

    admin_ids = await _admin_ids(bot, clone_id)
    if not admin_ids:
        return

    view = discord.ui.View(timeout=None)
    view.add_item(ReportChannelDMButton(clone_id))

    for admin_id in admin_ids:
        try:
            user = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            await user.send(
                "📮 One-time setup — I can forward every **\"report this server\"** submission "
                "from the site's directory straight into a channel (no more checking the DB by hand).\n\n"
                "Run `/setup reportchannel #channel-name` **in your server** to pick where they go "
                "(has to be run there, not here — Discord can only show me your server's channel "
                "list from inside the server itself), or tap the button below to just have them "
                "DMed to you instead:",
                view=view,
            )
            return  # only need one admin to configure it
        except (discord.HTTPException, discord.Forbidden):
            continue  # try the next admin


async def _admin_ids(bot, clone_id) -> list:
    """Reuses bot.py's own _owner_ids_for_alert logic rather than
    reimporting config constants here — keeps this in sync with whatever
    "who runs this bot" already means for join alerts."""
    try:
        return await bot._owner_ids_for_alert()
    except Exception:
        logger.exception("[report-channel-picker] couldn't resolve admin ids")
        return []


class ReportChannelSelect(
    discord.ui.DynamicItem[discord.ui.ChannelSelect],
    template=_id_pattern(),
):
    def __init__(self, clone_id):
        self.clone_id = clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Pick the reports channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode(clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        raw = int(match["clone_id"])
        return cls(None if raw == -1 else raw)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel = self.item.values[0]
        try:
            await db.set_report_notify_channel(self.clone_id, channel.guild.id, channel.id)
        except Exception:
            logger.exception("[report-channel-picker] failed saving channel for clone %s", self.clone_id)
            await interaction.edit_original_response(content="Something went wrong saving that — try picking again.", view=None)
            return
        await interaction.edit_original_response(
            content=f"✅ Done — reports will show up in {channel.mention} from now on. Any already waiting will land there shortly.",
            view=None,
        )


def _dm_encode(clone_id) -> str:
    return f"reportchan:dm:{clone_id if clone_id is not None else -1}"


def _dm_id_pattern() -> str:
    return r"reportchan:dm:(?P<clone_id>-?\d+)"


class ReportChannelDMButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_dm_id_pattern(),
):
    """The working half of the picker DM: a ChannelSelect can't populate
    any options outside a guild (see module docstring), so this button —
    wired to db.set_report_notify_dm — is the only choice this DM can
    actually offer. Picking a real channel now happens via /reportchannel,
    run inside the server (report_notifications.py)."""

    def __init__(self, clone_id):
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="DM me instead",
            style=discord.ButtonStyle.secondary,
            custom_id=_dm_encode(clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        raw = int(match["clone_id"])
        return cls(None if raw == -1 else raw)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await db.set_report_notify_dm(self.clone_id, interaction.user.id)
        except Exception:
            logger.exception("[report-channel-picker] failed saving DM fallback for clone %s", self.clone_id)
            await interaction.edit_original_response(content="Something went wrong saving that — try again.", view=None)
            return
        await interaction.edit_original_response(
            content="✅ Done — reports will be DMed to you here from now on.",
            view=None,
        )


DYNAMIC_ITEMS = (ReportChannelSelect, ReportChannelDMButton)
