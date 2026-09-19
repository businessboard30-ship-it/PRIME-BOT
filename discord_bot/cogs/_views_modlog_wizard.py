# FULL PATH: PRIME-BOT-main/discord_bot/cogs/_views_modlog_wizard.py

"""
/modlog — one wizard covering every server-activity log category (server,
channels, roles, members, moderation, voice, invites), built the same way as
_views_automod_wizard.py: LayoutView + Container + DynamicItems, restart-
proof, live-rerendering on every click.

Shares discord_automod_config.log_channel_id with /automod's own wizard —
picking a channel in either one covers both, so there's still only a single
"mod-log channel" concept per guild, just two front doors to it. The actual
category flags (log_server_enabled..log_invites_enabled) and the listeners
that read them live in discord_bot/cogs/server_logs.py.

All 7 categories default OFF (see database.py's discord_automod_config
comment). Per the approved design: the moment log_channel_id goes from unset
to set — or changes to a different channel — via EITHER wizard, this module
posts a fresh copy of this wizard straight into that channel so whoever set
it up can immediately flip categories on. See maybe_announce_new_log_channel,
called from _views_automod_wizard.py's channel select, automod.py's
/automod setlogchannel command, automod.py's ensure_log_channel auto-create
path, and this file's own channel select below.
"""

import re

import discord
from discord_bot import perm_check

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

CATEGORY_FIELD_MAP = {
    "server": "log_server_enabled",
    "channels": "log_channels_enabled",
    "roles": "log_roles_enabled",
    "members": "log_members_enabled",
    "moderation": "log_moderation_enabled",
    "voice": "log_voice_enabled",
    "invites": "log_invites_enabled",
}
CATEGORY_LABELS = {
    "server": "Server (name / icon / verification level)",
    "channels": "Channels (create / edit / delete)",
    "roles": "Roles (create / edit / delete / perms / color)",
    "members": "Members (nickname / avatar / username / roles)",
    "moderation": "Moderation (ban / unban / kick / timeout)",
    "voice": "Voice (join / leave / move)",
    "invites": "Invites (create / delete)",
}
CATEGORY_DESCRIPTIONS = {
    "server": "Name, icon, and verification-level changes",
    "channels": "Channel create, rename, delete, permission edits",
    "roles": "Role create, rename, delete, permission/color edits",
    "members": "Nickname/avatar/username changes, roles added/removed",
    "moderation": "Bans, unbans, kicks, timeouts — including manual ones",
    "voice": "Voice channel join / leave / move",
    "invites": "Invite links created or deleted, and by whom",
}


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _status_color(config: dict) -> discord.Color:
    if not config.get("log_channel_id"):
        return discord.Color.blurple()
    any_on = any(config.get(f) for f in CATEGORY_FIELD_MAP.values())
    return discord.Color.green() if any_on else discord.Color.greyple()


def render_status_lines(config: dict) -> list:
    log_id = config.get("log_channel_id")
    log = f"<#{log_id}>" if log_id else "*not set*"
    enabled = [CATEGORY_LABELS[c] for c, col in CATEGORY_FIELD_MAP.items() if config.get(col)]
    categories_label = "\n".join(f"  {'✅' if config.get(col) else '⬜'} {CATEGORY_LABELS[c]}" for c, col in CATEGORY_FIELD_MAP.items())

    step1 = "✅" if log_id else "⬜"
    step2 = "✅" if enabled else "⬜"

    return [
        f"{step1} **Step 1: Log channel** — {log}",
        f"{step2} **Step 2: Categories** — {len(enabled)}/7 enabled",
        categories_label,
    ]


# ---------------------------------------------------------------------------
# custom_id shape: mlwz_<field>:<guild_id>:<clone_id or "-">:<invoker_id or "-">
# ---------------------------------------------------------------------------

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"mlwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^mlwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "modlog", "manage_guild", "Manage Server")


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    """Renders a fresh wizard message from a config dict already fetched by
    the caller. Every dynamic item inside re-fetches its own current config
    on interaction rather than trusting this snapshot."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(config))

    chan_row = discord.ui.ActionRow()
    chan_row.add_item(ModlogChannelSelect(guild_id, clone_id, invoker_id))
    cat_row = discord.ui.ActionRow()
    cat_row.add_item(ModlogCategoriesSelect(guild_id, clone_id, invoker_id, config))

    text = discord.ui.TextDisplay(
        "\n".join([
            "### 📋 Set up server logging",
            *perm_check.lines(guild_id, clone_id),
            *render_status_lines(config),
            "",
            "-# Pick a category above to start logging it here. Reopen this panel anytime with `/modlog` — "
            "no need to repost it, this same message keeps working.",
        ])
    )
    for item in (text, discord.ui.Separator(), chan_row, cat_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_automod_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    """Called right after /modlog posts (or auto-posts) the wizard, so later
    changes made elsewhere (the other wizard, or a direct config write) can
    find this message again and push a live refresh to it."""
    await db.set_automod_config(
        guild_id, clone_id=clone_id,
        modlog_wizard_channel_id=channel_id, modlog_wizard_message_id=message_id,
        modlog_wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Best-effort, silent refresh of the last-posted /modlog wizard message
    after a change was made outside of it (e.g. /automod's channel select).
    Missing pointer, deleted channel/message, or no permission are all
    normal, non-error situations."""
    config = await db.get_automod_config(guild_id, clone_id=clone_id)
    channel_id = config.get("modlog_wizard_channel_id")
    message_id = config.get("modlog_wizard_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    invoker_raw = config.get("modlog_wizard_invoker_id")
    invoker_id = int(invoker_raw) if invoker_raw is not None else None
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def maybe_announce_new_log_channel(bot, guild_id: int, clone_id, invoker_id, old_channel_id, new_channel_id) -> None:
    """Fires whenever log_channel_id changes (first-set OR switched to a
    different channel) through ANY of: /automod's wizard channel select,
    /automod setlogchannel, the auto-create-on-join flow, the /automod
    reminder loop's channel backfill, or /modlog's own wizard channel
    select. Posts a fresh /modlog wizard straight into the newly-chosen
    channel so whoever set it up can flip categories on right there, then
    remembers it as the new pointer for future refreshes.

    Posts AT MOST ONE wizard message per channel: if a /modlog wizard is
    already tracked as living in this exact channel (and that message
    still exists), this is a no-op — refresh_posted_wizard already keeps
    that one live, so a second copy would just be duplicate clutter.

    Best-effort and silent: this is a convenience nudge, not a critical
    path, so missing permissions or a since-deleted channel just mean no
    wizard gets posted this time.
    """
    if not new_channel_id or str(new_channel_id) == str(old_channel_id):
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(int(new_channel_id))
    if channel is None:
        return

    config = await db.get_automod_config(guild_id, clone_id=clone_id)

    # Already have a live wizard message sitting in THIS channel? Don't
    # post a second one — just make sure it reflects current config.
    existing_channel_id = config.get("modlog_wizard_channel_id")
    existing_message_id = config.get("modlog_wizard_message_id")
    if existing_channel_id and str(existing_channel_id) == str(new_channel_id) and existing_message_id:
        try:
            await channel.fetch_message(int(existing_message_id))
            await refresh_posted_wizard(bot, guild_id, clone_id=clone_id)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # message is gone — fall through and post a fresh one

    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    try:
        message = await channel.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        return
    await remember_wizard_message(guild_id, clone_id, invoker_id, channel.id, message.id)


class ModlogChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("chan")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 1 — pick the mod-log channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("chan", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        old_config = await db.get_automod_config(self.guild_id, clone_id=self.clone_id)
        old_channel_id = old_config.get("log_channel_id")
        await db.set_automod_config(
            self.guild_id, clone_id=self.clone_id,
            log_channel_id=channel.id, log_channel_auto_created=False,
            log_channel_notice_count=0, log_channel_last_notice_at=None,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
        # This message is itself the most recently touched /modlog wizard —
        # remember it as the refresh pointer regardless of channel changes.
        sent = await interaction.original_response()
        await remember_wizard_message(self.guild_id, self.clone_id, self.invoker_id, sent.channel.id, sent.id)
        # Also push the same live update to /automod's wizard, since they
        # share this column.
        from discord_bot.cogs._views_automod_wizard import refresh_posted_wizard as refresh_automod_wizard
        await refresh_automod_wizard(interaction.client, self.guild_id, clone_id=self.clone_id)
        # If the picked channel is a DIFFERENT channel than the one this
        # wizard message is already sitting in, post a fresh copy straight
        # into it too, so setup can continue right there. If they're the
        # same channel (the common case: an admin runs /modlog inside the
        # channel they're about to designate as the log channel), this
        # very message already covers it — skip, or we'd post a duplicate
        # copy of the wizard right underneath itself.
        if channel.id != sent.channel.id:
            await maybe_announce_new_log_channel(
                interaction.client, self.guild_id, self.clone_id, self.invoker_id,
                old_channel_id, channel.id,
            )


class ModlogCategoriesSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("cats")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS[c], value=c,
                description=CATEGORY_DESCRIPTIONS[c],
                default=bool(config.get(col)),
            )
            for c, col in CATEGORY_FIELD_MAP.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 2 — pick which categories to log",
            options=options, min_values=0, max_values=len(options),
            custom_id=_encode("cats", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        selected = set(self.item.values)
        updates = {col: (c in selected) for c, col in CATEGORY_FIELD_MAP.items()}
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, **updates)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# Registered once in discord_bot/bot.py's setup_hook via
# bot.add_dynamic_items(*DYNAMIC_ITEMS), same mechanism as the automod
# wizard's DYNAMIC_ITEMS — keeps working after a restart regardless of which
# process originally posted the message.
DYNAMIC_ITEMS = (ModlogChannelSelect, ModlogCategoriesSelect)
