# path: discord_bot/cogs/_views_giveaway_wizard.py

"""
Bumper-style wizard for /giveaway setup.

Free tier: prize, duration, channel, winners, role requirement.
Premium tier (💎): scheduled start, custom embed color, bonus entries
per role, auto-reroll on winner inactivity.

All premium items are visible to everyone but locked with a 💎 badge and
a "Go Premium" upsell when the guild isn't subscribed — same pattern as
the join-DM feature list.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import discord

import config
from database import db
from discord_bot.cogs._views_shared import check_wizard_access

DURATION_CHOICES = [
    ("5m", 5 * 60), ("1h", 60 * 60), ("6h", 6 * 60 * 60),
    ("1d", 24 * 60 * 60), ("3d", 3 * 24 * 60 * 60), ("7d", 7 * 24 * 60 * 60),
]
WINNER_COUNT_CHOICES = [1, 2, 3, 5, 10]
COLOR_CHOICES = [
    ("Gold ⭐", "FFD700"), ("Red 🔴", "E74C3C"), ("Blue 💙", "3498DB"),
    ("Green 💚", "2ECC71"), ("Purple 💜", "9B59B6"), ("Pink 🌸", "FF69B4"),
    ("White ⬜", "FFFFFF"), ("Black ⬛", "000001"),
]
REROLL_HOUR_CHOICES = [1, 2, 6, 12, 24, 48]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _is_premium(guild_id: int, clone_id) -> bool:
    try:
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False


def _lock(label: str, premium: bool) -> str:
    return label if premium else f"💎 {label}"


def render_status_lines(draft: dict, premium: bool) -> list:
    prize = draft.get("prize")
    duration = draft.get("duration_seconds")
    duration_label = next((l for l, s in DURATION_CHOICES if s == duration), f"{duration}s" if duration else None)
    channel_id = draft.get("target_channel_id")
    winners = draft.get("winner_count", 1)
    role_id = draft.get("role_requirement_id")

    # Premium fields
    sched = draft.get("scheduled_start_at")
    sched_label = f"<t:{int(sched.timestamp())}:f>" if sched else None
    color = draft.get("embed_color")
    bonus = draft.get("bonus_entries_json")
    bonus_label = f"{len(json.loads(bonus))} role(s)" if bonus else None
    reroll = draft.get("auto_reroll_hours")

    lines = [
        f"{'✅' if prize else '⬜'} **Prize** — {prize or '*not set*'}",
        f"{'✅' if duration else '⬜'} **Duration** — {duration_label or '*not set*'}",
        f"{'✅' if channel_id else '⬜'} **Channel** — {f'<#{channel_id}>' if channel_id else '*not set*'}",
        f"✅ **Winners** — {winners}",
        f"{'✅' if role_id else '⬜'} **Role requirement** — {f'<@&{role_id}>' if role_id else 'none (optional)'}",
    ]
    if premium:
        lines += [
            "",
            "**💎 Premium options:**",
            f"{'✅' if sched_label else '⬜'} **Scheduled start** — {sched_label or 'not set (starts immediately)'}",
            f"{'✅' if color else '⬜'} **Embed color** — {'#' + color if color else 'default gold'}",
            f"{'✅' if bonus_label else '⬜'} **Bonus entries** — {bonus_label or 'none'}",
            f"{'✅' if reroll else '⬜'} **Auto-reroll** — {f'after {reroll}h inactivity' if reroll else 'off'}",
        ]
    else:
        lines += [
            "",
            "-# 💎 **Premium unlocks:** scheduled start, custom color, bonus entries, auto-reroll.",
        ]
    return lines


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


def build_wizard_view(wizard_message_id: int, draft: dict, premium: bool) -> discord.ui.LayoutView:
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

    # Premium rows — always visible, gated inside callback
    color_row = discord.ui.ActionRow()
    color_row.add_item(GiveawayColorSelect(wizard_message_id, draft, premium))

    reroll_row = discord.ui.ActionRow()
    reroll_row.add_item(GiveawayRerollSelect(wizard_message_id, draft, premium))

    bonus_row = discord.ui.ActionRow()
    bonus_row.add_item(GiveawayBonusEntriesButton(wizard_message_id, premium))

    sched_row = discord.ui.ActionRow()
    sched_row.add_item(GiveawayScheduleButton(wizard_message_id, premium))

    start_row = discord.ui.ActionRow()
    start_row.add_item(GiveawayStartButton(wizard_message_id))
    if not premium:
        start_row.add_item(GiveawayGoPremiumButton(wizard_message_id))

    text = discord.ui.TextDisplay("\n".join(["### 🎉 Create a giveaway", *render_status_lines(draft, premium)]))
    for item in (
        text, discord.ui.Separator(),
        prize_row, duration_row, channel_row, winners_row, role_row,
        discord.ui.Separator(),
        color_row, reroll_row, bonus_row, sched_row,
        discord.ui.Separator(),
        start_row,
    ):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, wizard_message_id: int):
    if not interaction.response.is_done():
        await interaction.response.defer()
    draft = await db.get_giveaway_draft(wizard_message_id) or {}
    guild_id = draft.get("guild_id") or (interaction.guild_id if interaction.guild else None)
    clone_id = draft.get("clone_id")
    premium = await _is_premium(guild_id, clone_id) if guild_id else False
    view = build_wizard_view(wizard_message_id, draft, premium)
    await interaction.edit_original_response(view=view)


async def _premium_gate(interaction: discord.Interaction, draft: dict) -> bool:
    """Returns True if guild is premium. If not, sends upsell and returns False."""
    guild_id = draft.get("guild_id") or (interaction.guild_id if interaction.guild else None)
    clone_id = draft.get("clone_id")
    if await _is_premium(guild_id, clone_id):
        return True
    from discord_bot.cogs._views_premium import send_premium_pitch
    await interaction.response.defer(ephemeral=True)
    await send_premium_pitch(interaction, guild_id, clone_id)
    return False


# ── Free-tier components ──────────────────────────────────────────────────

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
        await _rerender(interaction, self.wizard_message_id)


class GiveawayPrizeButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("prize")):
    def __init__(self, wizard_message_id: int):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label="✏️ Set prize", style=discord.ButtonStyle.secondary,
            custom_id=_encode("prize", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match))

    async def callback(self, interaction: discord.Interaction):
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
    async def from_custom_id(cls, interaction, item, match):
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
            min_values=1, max_values=1, custom_id=_encode("chan", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
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
    async def from_custom_id(cls, interaction, item, match):
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
            min_values=0, max_values=1, custom_id=_encode("role", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
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


# ── Premium components ────────────────────────────────────────────────────

class GiveawayColorSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("color")):
    def __init__(self, wizard_message_id: int, draft: dict, premium: bool):
        self.wizard_message_id = wizard_message_id
        current = draft.get("embed_color")
        options = [
            discord.SelectOption(label=label, value=hex_val, default=(hex_val == current))
            for label, hex_val in COLOR_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder=_lock("💎 Embed color", premium), options=options,
            custom_id=_encode("color", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match), {}, False)

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        if not await _premium_gate(interaction, draft):
            return
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), embed_color=self.item.values[0],
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayRerollSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("reroll")):
    def __init__(self, wizard_message_id: int, draft: dict, premium: bool):
        self.wizard_message_id = wizard_message_id
        current = draft.get("auto_reroll_hours")
        options = [discord.SelectOption(label="Off", value="0", default=(not current))]
        options += [
            discord.SelectOption(label=f"After {h}h inactivity", value=str(h), default=(h == current))
            for h in REROLL_HOUR_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder=_lock("💎 Auto-reroll", premium), options=options,
            custom_id=_encode("reroll", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match), {}, False)

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            return
        if not await _premium_gate(interaction, draft):
            return
        val = int(self.item.values[0])
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), auto_reroll_hours=val if val else None,
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayBonusEntriesModal(discord.ui.Modal, title="💎 Bonus entries (role:count per line)"):
    def __init__(self, wizard_message_id: int, current_json: str):
        super().__init__()
        self.wizard_message_id = wizard_message_id
        # Show current as role_id:extra_count lines
        try:
            rows = json.loads(current_json) if current_json else []
            default = "\n".join(f"{r['role_id']}:{r['extra_entries']}" for r in rows)
        except Exception:
            default = ""
        self.entries = discord.ui.TextInput(
            label="Role ID : extra entries (one per line)",
            placeholder="e.g.\n123456789012345678:2\n987654321098765432:1",
            default=default, style=discord.TextStyle.paragraph,
            required=False, max_length=500,
        )
        self.add_item(self.entries)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.followup.send("This wizard expired.", ephemeral=True)
            return
        parsed = []
        for line in self.entries.value.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) != 2:
                await interaction.followup.send(f"Bad line `{line}` — use `role_id:extra_entries`.", ephemeral=True)
                return
            try:
                role_id, extra = int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                await interaction.followup.send(f"Bad numbers in `{line}`.", ephemeral=True)
                return
            parsed.append({"role_id": role_id, "extra_entries": extra})
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), bonus_entries_json=json.dumps(parsed) if parsed else None,
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayBonusEntriesButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("bonus")):
    def __init__(self, wizard_message_id: int, premium: bool):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label=_lock("Bonus entries by role", premium),
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("bonus", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match), False)

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.response.send_message("Wizard expired.", ephemeral=True)
            return
        if not await _check_access(interaction, draft["invoker_id"]):
            return
        if not await _premium_gate(interaction, draft):
            return
        await interaction.response.send_modal(
            GiveawayBonusEntriesModal(self.wizard_message_id, draft.get("bonus_entries_json"))
        )


class GiveawayScheduleModal(discord.ui.Modal, title="💎 Schedule start (UTC)"):
    def __init__(self, wizard_message_id: int):
        super().__init__()
        self.wizard_message_id = wizard_message_id
        self.when = discord.ui.TextInput(
            label="Start date/time (YYYY-MM-DD HH:MM UTC)",
            placeholder="e.g. 2026-10-01 18:00",
            max_length=20,
        )
        self.add_item(self.when)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.followup.send("Wizard expired.", ephemeral=True)
            return
        try:
            dt = datetime.strptime(self.when.value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            await interaction.followup.send("Invalid format — use `YYYY-MM-DD HH:MM`.", ephemeral=True)
            return
        if dt <= datetime.now(timezone.utc) + timedelta(minutes=1):
            await interaction.followup.send("Start time must be in the future.", ephemeral=True)
            return
        await db.upsert_giveaway_draft(
            self.wizard_message_id, draft["guild_id"], draft["wizard_channel_id"], draft["invoker_id"],
            clone_id=draft.get("clone_id"), scheduled_start_at=dt,
        )
        await _rerender(interaction, self.wizard_message_id)


class GiveawayScheduleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("sched")):
    def __init__(self, wizard_message_id: int, premium: bool):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label=_lock("Schedule start", premium),
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("sched", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match), False)

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None:
            await interaction.response.send_message("Wizard expired.", ephemeral=True)
            return
        if not await _check_access(interaction, draft["invoker_id"]):
            return
        if not await _premium_gate(interaction, draft):
            return
        await interaction.response.send_modal(GiveawayScheduleModal(self.wizard_message_id))


class GiveawayGoPremiumButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("goprem")):
    def __init__(self, wizard_message_id: int):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label=f"Go Premium 💎 — ${config.PREMIUM_FEE_USD:g}/month",
            style=discord.ButtonStyle.primary,
            custom_id=_encode("goprem", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match))

    async def callback(self, interaction: discord.Interaction):
        draft = await db.get_giveaway_draft(self.wizard_message_id) or {}
        guild_id = draft.get("guild_id") or (interaction.guild_id if interaction.guild else None)
        clone_id = draft.get("clone_id")
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, guild_id, clone_id)


# ── Start button ──────────────────────────────────────────────────────────

class GiveawayStartButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("start")):
    def __init__(self, wizard_message_id: int):
        self.wizard_message_id = wizard_message_id
        super().__init__(discord.ui.Button(
            label="🎉 Start giveaway", style=discord.ButtonStyle.success,
            custom_id=_encode("start", wizard_message_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(_decode(match))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from discord_bot.cogs.giveaways import GiveawayEntryView, _giveaway_embed

        draft = await db.get_giveaway_draft(self.wizard_message_id)
        if draft is None or not await _check_access(interaction, draft["invoker_id"]):
            if draft is None:
                await interaction.followup.send("This wizard expired — run `/giveaway setup` again.", ephemeral=True)
            return

        missing = [
            name for name, val in (
                ("prize", draft.get("prize")),
                ("duration", draft.get("duration_seconds")),
                ("channel", draft.get("target_channel_id")),
            ) if not val
        ]
        if missing:
            await interaction.followup.send(f"Still missing: {', '.join(missing)}.", ephemeral=True)
            return

        channel = interaction.client.get_channel(int(draft["target_channel_id"]))
        if channel is None:
            await interaction.followup.send("Couldn't find that channel — pick it again.", ephemeral=True)
            return

        ends_at = datetime.now(timezone.utc) + timedelta(seconds=draft["duration_seconds"])

        # Build color from premium field if set
        color = None
        if draft.get("embed_color"):
            try:
                color = discord.Color(int(draft["embed_color"], 16))
            except (ValueError, TypeError):
                pass

        embed = _giveaway_embed(
            draft["prize"], draft["winner_count"], ends_at, interaction.user, 0,
            role_requirement_id=draft.get("role_requirement_id"),
            color=color,
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
            clone_id=draft.get("clone_id"),
            role_requirement_id=draft.get("role_requirement_id"),
            embed_color=draft.get("embed_color"),
            bonus_entries_json=draft.get("bonus_entries_json"),
            auto_reroll_hours=draft.get("auto_reroll_hours"),
        )
        await db.delete_giveaway_draft(self.wizard_message_id)

        confirm_view = discord.ui.LayoutView(timeout=None)
        confirm_container = discord.ui.Container(accent_colour=discord.Color.green())
        confirm_container.add_item(discord.ui.TextDisplay(
            f"### 🎉 Giveaway started in {channel.mention}!\n"
            f"**{draft['prize']}** — ends <t:{int(ends_at.timestamp())}:R>"
        ))
        confirm_view.add_item(confirm_container)
        await interaction.edit_original_response(view=confirm_view)


DYNAMIC_ITEMS = (
    GiveawayPrizeButton, GiveawayDurationSelect, GiveawayChannelSelect,
    GiveawayWinnerCountSelect, GiveawayRoleRequirementSelect,
    GiveawayColorSelect, GiveawayRerollSelect,
    GiveawayBonusEntriesButton, GiveawayScheduleButton,
    GiveawayGoPremiumButton, GiveawayStartButton,
)
