# path: discord_bot/cogs/_views_automod_reminders.py

"""
Buttons on the COMBINED automod reminder DM sent by
AutomodCog._reminder_loop (discord_bot/cogs/automod.py).

Before this, the log-channel notice and the word-filter notice were two
independent standalone DMs (_notify_owner_log_channel /
_notify_owner_word_filter), each fired per-guild. An owner of two guilds
could get all four in the same tick — worse, right after any bot downtime,
the loop's startup pass catches up on every guild whose reminder cooldown
elapsed while it was offline and fires them all at once (same burst shape
as the bump-reminder crash-loop incident this mirrors the fix for).

_reminder_loop now collects every pending item (per guild, per type) across
a whole tick, groups them by owner, and sends ONE DM per owner containing
one embed field per item plus a single "Remind me later" / "Don't ask
again" button pair that applies to every item in that batch at once.

Built with discord.ui.DynamicItem (not a plain View + in-memory state), so
the buttons keep working across bot restarts — same pattern as
_views_join_dm.py's _RemindLaterButton / _DontAskAgainButton. The only
per-message state is the small integer batch id (from
discord_automod_reminder_batches — see database.py), encoded straight into
each button's custom_id; everything else (which guilds, which notice
types) is looked up from that row at click time rather than baked into the
component tree, so it stays correct even if the DB row changes shape
later.

Each button class gets its OWN custom_id template (rather than sharing one
regex the way the join-DM buttons do) to avoid any ambiguity about which
class's from_custom_id should win when two templates could both match the
same string — a real edge case (two DynamicItem classes almost never need
literally identical templates) and this file has no reason to risk it.
"""

import re

import discord

from database import db

_LATER_RE = re.compile(r"^automod_rem_later:(\d+)$")
_STOP_RE = re.compile(r"^automod_rem_stop:(\d+)$")


def _disabled_view(interaction: discord.Interaction) -> discord.ui.View:
    """Rebuilds the two buttons from the message that was just clicked, all
    disabled — same trick _views_join_dm.py's _disabled_view uses, so a
    second click (or a click after the batch already got actioned) can't
    double-apply the effect."""
    view = discord.ui.View(timeout=None)
    for row in interaction.message.components:
        for child in getattr(row, "children", [row]):
            if not isinstance(child, discord.Button) or not child.custom_id:
                continue
            view.add_item(discord.ui.Button(
                label=child.label, style=child.style, emoji=child.emoji,
                custom_id=child.custom_id, disabled=True,
            ))
    return view


def build_reminder_view(batch_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_ReminderLaterButton(batch_id))
    view.add_item(_ReminderDismissButton(batch_id))
    return view


async def _apply_to_batch(batch_id: int, *, later: bool) -> bool:
    """Shared logic for both buttons: loads the batch's item list and
    updates every guild row it references. Returns False if the batch is
    gone (already cleaned up by /automod owner cleanupreminders, or a
    stale/duplicate click), in which case the caller just disables the
    view and stops."""
    batch = await db.get_automod_reminder_batch(batch_id)
    if batch is None:
        return False
    clone_id = batch["clone_id"]
    now = discord.utils.utcnow()
    for item in batch["items"]:
        # "max_notices" is baked into each item when the batch is created
        # (see automod.py's _send_combined_reminder) rather than imported
        # from automod.py's MAX_LOG_CHANNEL_NOTICES / MAX_WORDFILTER_NOTICES
        # constants — this module can't import those (automod.py imports
        # build_reminder_view from here, so importing back would be
        # circular), and baking in the value the batch was actually built
        # with is more correct anyway if the caps ever change between when
        # a batch was sent and when it's clicked.
        max_notices = item.get("max_notices", 3)
        if item["type"] == "log_channel":
            if later:
                await db.set_automod_config(item["guild_id"], clone_id=clone_id, log_channel_last_notice_at=now)
            else:
                await db.set_automod_config(item["guild_id"], clone_id=clone_id, log_channel_notice_count=max_notices)
        else:
            if later:
                await db.set_automod_config(item["guild_id"], clone_id=clone_id, wordfilter_last_notice_at=now)
            else:
                await db.set_automod_config(item["guild_id"], clone_id=clone_id, wordfilter_notice_count=max_notices)
    await db.mark_automod_reminder_batch_resolved(batch_id)
    return True


class _ReminderLaterButton(discord.ui.DynamicItem[discord.ui.Button], template=_LATER_RE.pattern):
    def __init__(self, batch_id: int):
        self.batch_id = batch_id
        super().__init__(discord.ui.Button(
            label="Remind me later", style=discord.ButtonStyle.secondary,
            emoji="⏰", custom_id=f"automod_rem_later:{batch_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await _apply_to_batch(self.batch_id, later=True)
        # Guard against a duplicate INTERACTION_CREATE dispatch — see
        # _ReminderDismissButton below for why this can fire twice.
        if interaction.response.is_done():
            return
        try:
            await interaction.response.edit_message(view=_disabled_view(interaction))
        except discord.HTTPException:
            return
        await interaction.followup.send("Got it — I'll check back in a couple days.", ephemeral=True)


class _ReminderDismissButton(discord.ui.DynamicItem[discord.ui.Button], template=_STOP_RE.pattern):
    def __init__(self, batch_id: int):
        self.batch_id = batch_id
        super().__init__(discord.ui.Button(
            label="Don't ask again", style=discord.ButtonStyle.danger,
            emoji="🚫", custom_id=f"automod_rem_stop:{batch_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await _apply_to_batch(self.batch_id, later=False)
        # Guard against a duplicate INTERACTION_CREATE dispatch — Discord
        # occasionally redelivers the same interaction during a gateway
        # resume (observed in production logs alongside "WebSocket ...
        # ratelimited" / repeated reconnects), which previously crashed
        # this callback with "Interaction has already been acknowledged"
        # on edit_message().
        if interaction.response.is_done():
            return
        try:
            await interaction.response.edit_message(view=_disabled_view(interaction))
        except discord.HTTPException:
            return
        await interaction.followup.send("Understood — I won't message you about these again.", ephemeral=True)


# Registered once in discord_bot/bot.py's setup_hook via
# bot.add_dynamic_items(*DYNAMIC_ITEMS), same mechanism as every other
# wizard's DYNAMIC_ITEMS.
DYNAMIC_ITEMS = (_ReminderLaterButton, _ReminderDismissButton)
