# path: discord_bot/cogs/_views_invites.py

"""
Invite tracker setup wizard. Posted automatically some time after the
bot joins a server (InvitesCog._scheduler_loop, delayed rather than
immediate — see invites.py), and re-postable any time via /invites setup
for when the auto-posted one gets buried or deleted. Setup itself is
still all buttons, no typed arguments — the slash command's only job is
to summon the same wizard, not replace it.

Shape mirrors _views_download_wizard.py's "auto-create OR pick a
channel" pattern exactly, just pointed at the invite tracker's announce
channel instead of a downloads channel, plus an enable/disable toggle
and a leaderboard button.

Built entirely from discord.ui.DynamicItem (same reasoning as every
other wizard here — see _views_welcome.py's module docstring for the
full explanation): timeout=None, no expiry, every component re-fetches
its own current config from the DB rather than trusting anything baked
into the message at render time, so a click on a wizard message posted
before a restart still works identically after one.
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    # invoker_id is None for the auto-posted wizard (no specific admin ran
    # a command) and set to the invoking user's id for one posted via
    # /invites setup — check_wizard_access handles both: None falls back
    # to the permission check, a real id restricts to just that user.
    return await check_wizard_access(interaction, invoker_id, "invites", "manage_guild", "Manage Server")


def _id_pattern(field: str) -> str:
    return rf"^invwz_{field}:(\d+):(-|\d+):(-|\d+)$"


def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"invwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    invoker_id = None if match.group(3) == "-" else int(match.group(3))
    return guild_id, clone_id, invoker_id


def render_status_lines(config: dict) -> list:
    channel_id = config.get("channel_id")
    enabled = config.get("enabled", True)
    lines = []
    if channel_id:
        lines.append(f"✅ **Announce channel** — <#{channel_id}>")
    else:
        lines.append("⬜ **Announce channel** — not set up yet. Create one or pick an existing channel below.")
    lines.append(
        f"{'✅' if enabled else '🚫'} **Join announcements** — "
        f"{'on' if enabled else 'off (invites are still tracked quietly for the leaderboard)'}"
    )
    return lines


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    """Builds a fresh wizard message from an already-fetched config dict.
    Every dynamic item re-fetches its own current config on interaction —
    this is only ever used to render, never to hold state between clicks."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    text = discord.ui.TextDisplay("\n".join([
        "### 🔗 Invite Tracker",
        "See who invited each new member, right in this server.",
        *render_status_lines(config),
    ]))
    container.add_item(text)
    container.add_item(discord.ui.Separator())

    create_row = discord.ui.ActionRow()
    create_row.add_item(InviteCreateChannelButton(guild_id, clone_id, invoker_id))
    container.add_item(create_row)

    select_row = discord.ui.ActionRow()
    select_row.add_item(InviteChannelSelect(guild_id, clone_id, invoker_id))
    container.add_item(select_row)

    button_row = discord.ui.ActionRow()
    button_row.add_item(InviteToggleButton(guild_id, clone_id, invoker_id, config))
    button_row.add_item(InviteLeaderboardButton(guild_id, clone_id, invoker_id))
    container.add_item(button_row)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_invite_tracker_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_invite_tracker_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Best-effort re-push of the DB's current state onto an already-posted
    wizard message — same convention/reasoning as every other wizard's
    refresh_posted_wizard (see _views_welcome.py's for the full writeup).
    Not currently called from anywhere since every state change here
    already happens through this wizard's own buttons (which self-rerender),
    but kept for parity in case that changes later."""
    config = await db.get_invite_tracker_config(guild_id, clone_id=clone_id)
    channel_id = config.get("wizard_channel_id")
    message_id = config.get("wizard_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    invoker_raw = config.get("wizard_invoker_id")
    invoker_id = int(invoker_raw) if invoker_raw is not None else None
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


class InviteCreateChannelButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("create")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✨ Create #invites channel", style=discord.ButtonStyle.success,
            custom_id=_encode("create", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _check_access(interaction, self.invoker_id):
            return
        guild = interaction.guild
        try:
            channel = await guild.create_text_channel("invites", reason="Invite tracker setup")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to create channels here.", ephemeral=True)
            return
        await db.set_invite_tracker_config(
            self.guild_id, clone_id=self.clone_id, channel_id=channel.id, channel_auto_created=True,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class InviteChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("pick")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="...or pick an existing channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("pick", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        channel = self.item.values[0]
        await db.set_invite_tracker_config(
            self.guild_id, clone_id=self.clone_id, channel_id=channel.id, channel_auto_created=False,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class InviteToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("toggle")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        enabled = config.get("enabled", True)
        super().__init__(discord.ui.Button(
            label=("🔕 Turn off announcements" if enabled else "🔔 Turn on announcements"),
            style=(discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.success),
            custom_id=_encode("toggle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        config = await db.get_invite_tracker_config(self.guild_id, clone_id=self.clone_id)
        await db.set_invite_tracker_config(
            self.guild_id, clone_id=self.clone_id, enabled=not config.get("enabled", True),
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class InviteLeaderboardButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("board")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="🏆 Leaderboard", style=discord.ButtonStyle.primary,
            custom_id=_encode("board", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        # Viewing the leaderboard is read-only, so it's open to anyone in
        # the server, not gated behind Manage Server like the setup
        # buttons above — same reasoning as e.g. leveling's /leaderboard.
        await interaction.response.defer(ephemeral=True)
        rows = await db.get_invite_leaderboard(self.guild_id, clone_id=self.clone_id, limit=10)
        if not rows:
            await interaction.followup.send(
                "No tracked invites yet — nobody's joined through a trackable invite so far.", ephemeral=True,
            )
            return
        lines = []
        for i, (inviter_id, joins, net) in enumerate(rows, start=1):
            left = joins - net
            left_note = f", {left} left" if left else ""
            lines.append(f"**{i}.** <@{inviter_id}> — **{net}** invite{'s' if net != 1 else ''}{left_note}")
        await interaction.followup.send("🏆 **Invite leaderboard**\n" + "\n".join(lines), ephemeral=True)


DYNAMIC_ITEMS = (
    InviteCreateChannelButton, InviteChannelSelect, InviteToggleButton, InviteLeaderboardButton,
)
