# path: discord_bot/cogs/_views_giveaway_wizard.py

"""
Bumper-style wizard for /giveaway setup — matches giveaway_setup_wizard.html.

Structurally different from the other five wizards: those edit an
existing per-guild config row. A giveaway isn't configuration, it's a
one-shot creation flow — there's nothing in discord_giveaways to point
at until "Start giveaway" is actually pressed. State lives in
discord_giveaway_drafts, keyed by the wizard message itself (see
database.py's migration comment for why per-message instead of
per-guild), so this stays exactly as restart-proof as the others: every
component re-reads the draft row fresh on click rather than trusting
anything held in memory.
"""

import re
from datetime import datetime, timedelta, timezone

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

DURATION_CHOICES = [
    ("5m", 5 * 60), ("1h", 60 * 60), ("6h", 6 * 60 * 60),
    ("1d", 24 * 60 * 60), ("3d", 3 * 24 * 60 * 60), ("7d", 7 * 24 * 60 * 60),
]
WINNER_COUNT_CHOICES = [1, 2, 3, 5, 10]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def render_status_lines(draft: dict) -> list:
    prize = draft.get("prize")
    duration = draft.get("duration_seconds")
    duration_label = next((label for label, secs in DURATION_CHOICES if secs == duration), f"{duration}s" if duration else None)
    channel_id = draft.get("target_channel_id")
    winners = draft.get("winner_count", 1)
    role_id = draft.get("role_requirement_id")

    return [
        f"{'✅' if prize else '⬜'} **Prize** — {prize or '*not set*'}",
        f"{'✅' if duration else '⬜'} **Duration** — {duration_label or '*not set*'}",
        f"{'✅' if channel_id else '⬜'} **Channel** — {f'<#{channel_id}>' if channel_id else '*not set*'}",
        f"✅ **Winners** — {winners}",
        f"{'✅' if role_id else '⬜'} **Role requirement** — {f'<@&{role_id}>' if role_id else 'none (optional)'}",
    ]


def _id_pattern(field: str) -> str:
    return rf"^gwwz_{field}:(\d+)$"


def _encode(field: str, wizard_message_id: int) -> str:
    return f"gwwz_{field}:{wizard_message_id}"


def _decode(match: "re.Match") -> int:
    return int(match.group(1))


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(
        interaction, invoker_id, "giveaway", "manage_guild", "Manage Server", admin_override=True
    )


def build_wizard_view(wizard_message_id: int, draft: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.gold())

    prize_row = discord.ui.ActionRow()
    prize_row.add_item(GiveawayPrizeButton(wizard_message_id))
    duration_row = discord.ui.ActionRow()
    duration_row.add_item(GiveawayDurationSelect(wizard_message_id, draft))
    channel_row = discord.ui.ActionRow()
    channel_row.add_item(GiveawayChannelSelect(wizard_message_id, draft))
    winners_row = discord.ui.ActionRow()
    winners_row.add_item(GiveawayWinnerCountSelect(wizard_message_id, draft))
    role_row = discord.ui.ActionRow()
    role_row.add_item(GiveawayRoleRequirementSelect(wizard_message_id, draft))
    start_row = discord.ui.ActionRow()
    start_row.add_item(GiveawayStartButton(wizard_message_id))

    text = discord.ui.TextDisplay("\n".join(["### 🎉 Create a giveaway", *render_status_lines(draft)]))
    for item in (text, discord.ui.Separator(), prize_row, duration_row, channel_row, winners_row, role_row, discord.ui.Separator(), start_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, wizard_message_id: int):
    # is_done() guard: GiveawayStartButton defers before calling in (it needs
    # to do async work first), so a second unconditional defer() here would
    # raise InteractionResponded.
    if not interaction.response.is_done():
        await interaction.response.defer()
    draft = await db.get_giveaway_draft(wizard_message_id) or {}
    view = build_wizard_view(wizard_message_id, draft)
    await interaction.edit_original_response(view=view)


class GiveawayPrizeModal(discord.ui.Modal, title="What's being given away?"):
    def __init__(self, wizard_message_id: int, current: str):
        super().__init__()
        self.wizard_message_id = wizard_message_id
        self.prize = discord.ui.TextInput(label="Prize", default=current or "", max_length=200)
        self.add_item(self.prize)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.followup.send("This wizard expired — run `/giveaway setup` again.", ephemeral=True)
            return
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), prize=str(self.prize.value),
        )
        updated = await db.get_giveaway_draft(self.wizard_message_id)
        view = build_wizard_view(self.wizard_message_id, updated)
        await interaction.edit_original_response(view=view)


class GiveawayPrizeButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("prize")):
    def __init__(self, wizard_message_id: int):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label="✏️ Set prize", style=discord.ButtonStyle.secondary,
            custom_id=_encode("prize", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match))

    async def callback(self, interaction: discord.Interaction):
        # No defer() here: send_modal must be the *first* response to the
        # interaction (same class of bug as bump_edit — defer-then-modal
        # raises InteractionResponded). check_wizard_access also replies via
        # response.send_message on denial, so it likewise needs the
        # interaction un-acked when it's called.
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.response.send_message("This wizard expired — run `/giveaway setup` again.", ephemeral=True)
            return
        if not await _check_access(interaction, draft["invoker_id"]):
            return
        await interaction.response.send_modal(GiveawayPrizeModal(self.wizard_message_id, draft.get("prize")))


class GiveawayDurationSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("duration")):
    def __init__(self, wizard_message_id: int, draft: dict):
        self.wizard_message_id = wizard_message_id
        current = draft.get("duration_seconds")
        options = [
            discord.SelectOption(label=label, value=str(secs), default=(secs == current))
            for label, secs in DURATION_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Duration (5m / 1h / 6h / 1d / 3d / 7d)", options=options,
            custom_id=_encode("duration", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match), {})

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), duration_seconds=int(self.item.values[0]),
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("chan")):
    def __init__(self, wizard_message_id: int, draft: dict):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Pick channel", channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("chan", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match), {})

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        channel = self.item.values[0]
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), target_channel_id=channel.id,
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayWinnerCountSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("winners")):
    def __init__(self, wizard_message_id: int, draft: dict):
        self.wizard_message_id = wizard_message_id
        current = draft.get("winner_count", 1)
        options = [
            discord.SelectOption(label=f"{n} winner(s)", value=str(n), default=(n == current))
            for n in WINNER_COUNT_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Winner count", options=options,
            custom_id=_encode("winners", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match), {})

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), winner_count=int(self.item.values[0]),
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayRoleRequirementSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_id_pattern("role")):
    def __init__(self, wizard_message_id: int, draft: dict):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="Role requirement (optional) — leave empty for none",
            min_values=0, max_values=1,
            custom_id=_encode("role", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match), {})

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        role_id = self.item.values[0].id if self.item.values else None
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), role_requirement_id=role_id,
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayStartButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("start")):
    def __init__(self, wizard_message_id: int):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label="🎉 Start giveaway", style=discord.ButtonStyle.success,
            custom_id=_encode("start", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(_decode(match))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Imported here to avoid a cross-cog import cycle: giveaways.py
        # imports this module to launch the wizard.
        from discord_bot.cogs.giveaways import GiveawayEntryView, _giveaway_embed

        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            if draft is None:
                await interaction.followup.send("This wizard expired — run `/giveaway setup` again.", ephemeral=True)
            return

        missing = [name for name, val in (("prize", draft.get("prize")), ("duration", draft.get("duration_seconds")), ("channel", draft.get("target_channel_id"))) if not val]
        if missing:
            await interaction.followup.send(f"Still missing: {', '.join(missing)}.", ephemeral=True)
            return

        channel = interaction.client.get_channel(int(draft["target_channel_id"]))
        if channel is None:
            await interaction.followup.send("Couldn't find that channel — pick it again.", ephemeral=True)
            return

        ends_at = datetime.now(timezone.utc) + timedelta(seconds=draft["duration_seconds"])
        embed = _giveaway_embed(
            draft["prize"], draft["winner_count"], ends_at, interaction.user, 0,
            role_requirement_id=draft.get("role_requirement_id"),
        )
        cog = interaction.client.get_cog("GiveawayCog")
        try:
            posted = await channel.send(embed=embed, view=GiveawayEntryView(cog))
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"Couldn't post there: {e}", ephemeral=True)
            return

        await db.create_giveaway(
            draft["guild_id"], channel.id, posted.id, draft["invoker_id"],
            draft["prize"], draft["winner_count"], ends_at,
            clone_id=draft.get("clone_id"), role_requirement_id=draft.get("role_requirement_id"),
        )
        await db.delete_giveaway_draft(self.wizard_message_id)

        confirm_view = discord.ui.LayoutView(timeout=None)
        confirm_container = discord.ui.Container(accent_colour=discord.Color.green())
        confirm_container.add_item(discord.ui.TextDisplay(f"### 🎉 Giveaway started in {channel.mention}!\n**{draft['prize']}** — ends <t:{int(ends_at.timestamp())}:R>"))
        confirm_view.add_item(confirm_container)
        await interaction.edit_original_response(view=confirm_view)


DYNAMIC_ITEMS = (
    GiveawayPrizeButton, GiveawayDurationSelect, GiveawayChannelSelect,
    GiveawayWinnerCountSelect, GiveawayRoleRequirementSelect, GiveawayStartButton,
)
