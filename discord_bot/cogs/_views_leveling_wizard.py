# path: discord_bot/cogs/_views_leveling_wizard.py

"""
Bumper-style setup wizard for /leveling setup. Same DynamicItem/
restart-proof pattern as the other four wizards.

Adding a role reward is a two-step flow because Discord modals can only
contain text inputs (no role picker inside a modal): the "Add reward"
button opens a modal asking for the level number, and on submit that
posts a SEPARATE ephemeral follow-up containing a RoleSelect — picking a
role there is what actually calls db.add_level_role and refreshes the
main wizard message. The level number rides along inside that RoleSelect
component's own custom_id.
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

XP_RATE_LABELS = {"slow": "Slow", "default": "Default (10/min)", "fast": "Fast"}
XP_RATE_ORDER = ["slow", "default", "fast"]

CARD_STYLE_LABELS = {"card": "🖼️ Image card", "text": "💬 Text only", "off": "🔕 Off"}
CARD_STYLE_ORDER = ["card", "text", "off"]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def render_status_lines(config: dict, role_rows: list) -> list:
    rate_label = XP_RATE_LABELS.get(config.get("xp_rate", "default"), "Default (10/min)")
    announce_id = config.get("announce_channel_id")
    card_label = CARD_STYLE_LABELS.get(config.get("card_style", "card"), CARD_STYLE_LABELS["card"])
    return [
        f"✅ **Step 1: XP rate** — {rate_label}",
        f"{'✅' if announce_id else '⬜'} **Step 2: Announce channel** — {f'<#{announce_id}>' if announce_id else '*posts in the channel that triggered it*'}",
        f"✅ **Step 3: Level-up announcement** — {card_label}",
        f"{'✅' if role_rows else '⬜'} **Step 4: Role rewards** — {len(role_rows)} configured" if role_rows else "⬜ **Step 4: Role rewards** — none configured",
    ]


def _id_pattern(field: str) -> str:
    return rf"^lvlwz_{field}:(\d+):(-|\d+):(-|\d+)$"


def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"lvlwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern_with_level(field: str) -> str:
    return rf"^lvlwz_{field}:(\d+):(-|\d+):(-|\d+):(\d+)$"


def _encode_with_level(field: str, guild_id: int, clone_id, invoker_id, level: int) -> str:
    return f"{_encode(field, guild_id, clone_id, invoker_id)}:{level}"


def _decode_with_level(match: "re.Match"):
    guild_id, clone_id, invoker_id = _decode(match)
    level = int(match.group(4))
    return guild_id, clone_id, invoker_id, level


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "leveling", "manage_roles", "Manage Roles")


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict, role_rows: list) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    rate_row = discord.ui.ActionRow()
    rate_row.add_item(LevelingXpRateSelect(guild_id, clone_id, invoker_id, config))
    announce_row = discord.ui.ActionRow()
    announce_row.add_item(LevelingAnnounceChannelSelect(guild_id, clone_id, invoker_id, config))
    card_row = discord.ui.ActionRow()
    card_row.add_item(LevelingCardStyleSelect(guild_id, clone_id, invoker_id, config))

    text = discord.ui.TextDisplay("\n".join(["### 📈 Set up leveling", *render_status_lines(config, role_rows)]))
    items = [text, discord.ui.Separator(), rate_row, announce_row, card_row]

    if role_rows:
        lines = "\n".join(f"Level {r['level']} → <@&{r['role_id']}>" for r in role_rows[:10])
        items.append(discord.ui.TextDisplay(lines))

    add_row = discord.ui.ActionRow()
    add_row.add_item(LevelingAddRewardButton(guild_id, clone_id, invoker_id))
    items.extend([discord.ui.Separator(), add_row])

    for item in items:
        container.add_item(item)
    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    # is_done() guard: some callers (e.g. toggle/action buttons that do
    # async work before this) already defer()/respond before calling in —
    # calling response.defer() again would raise InteractionResponded.
    #
    # That guard is check-then-act, though: Discord can redeliver the same
    # interaction to two concurrent callback invocations, and both can see
    # is_done() == False before either has actually deferred. The second
    # defer() then hits the API after the first already acknowledged it,
    # raising HTTPException 40060 ("Interaction has already been
    # acknowledged"). Treat that as "someone else already deferred" instead
    # of letting it propagate and drop the re-render on the floor.
    if not interaction.response.is_done():
        try:
            await interaction.response.defer()
        except discord.HTTPException as e:
            if getattr(e, "code", None) != 40060:
                raise
    config = await db.get_leveling_config(guild_id, clone_id=clone_id)
    role_rows = await db.get_level_roles(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config, role_rows)
    try:
        await interaction.edit_original_response(view=view)
    except discord.HTTPException:
        # Original response may not exist yet if defer() itself lost the
        # race entirely; fall back to a followup edit of the same message.
        try:
            await interaction.followup.edit_message("@original", view=view)
        except discord.HTTPException:
            pass


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_leveling_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    config = await db.get_leveling_config(guild_id, clone_id=clone_id)
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
    role_rows = await db.get_level_roles(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config, role_rows)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


class LevelingXpRateSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("rate")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("xp_rate", "default")
        options = [
            discord.SelectOption(label=XP_RATE_LABELS[r], value=r, default=(r == current))
            for r in XP_RATE_ORDER
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 1 — XP rate (Slow / Default / Fast)", options=options,
            custom_id=_encode("rate", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_leveling_config(self.guild_id, clone_id=self.clone_id, xp_rate=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class LevelingAnnounceChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("announce")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 2 — pick announce channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("announce", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        await db.set_leveling_config(self.guild_id, clone_id=self.clone_id, announce_channel_id=channel.id, announce_auto_created=False)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class LevelingCardStyleSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("cardstyle")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("card_style", "card")
        options = [
            discord.SelectOption(label=CARD_STYLE_LABELS[s], value=s, default=(s == current))
            for s in CARD_STYLE_ORDER
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 3 — level-up announcement style",
            options=options,
            custom_id=_encode("cardstyle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_leveling_config(self.guild_id, clone_id=self.clone_id, card_style=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class LevelingRewardLevelModal(discord.ui.Modal, title="Add a level-up role reward"):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.level = discord.ui.TextInput(label="At what level?", max_length=4, placeholder="e.g. 10")
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            level = int(str(self.level.value))
            if level < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Level needs to be a whole number, 1 or higher.", ephemeral=True)
            return
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"Now pick the role to grant at **level {level}**:"))
        row = discord.ui.ActionRow()
        row.add_item(LevelingRewardRoleSelect(self.guild_id, self.clone_id, self.invoker_id, level))
        container.add_item(row)
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)


class LevelingRewardRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_id_pattern_with_level("rewardrole")):
    def __init__(self, guild_id: int, clone_id, invoker_id, level: int):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.level = level
        super().__init__(discord.ui.RoleSelect(
            placeholder=f"Role to grant at level {level}",
            min_values=1, max_values=1,
            custom_id=_encode_with_level("rewardrole", guild_id, clone_id, invoker_id, level),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id, level = _decode_with_level(match)
        return cls(guild_id, clone_id, invoker_id, level)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _check_access(interaction, self.invoker_id):
            return
        role = self.item.values[0]
        if interaction.guild and role >= interaction.guild.me.top_role:
            await interaction.followup.send(
                "That role is above (or equal to) my own top role — move my role above it first.", ephemeral=True
            )
            return
        ok = await db.add_level_role(self.guild_id, self.level, role.id, clone_id=self.clone_id)
        result_view = discord.ui.LayoutView(timeout=None)
        result_container = discord.ui.Container()
        result_container.add_item(discord.ui.TextDisplay(
            f"✅ Level {self.level} now grants **{role.name}**." if ok else "❌ Couldn't save that."
        ))
        result_view.add_item(result_container)
        await interaction.edit_original_response(view=result_view)
        from discord_bot.cogs._views_leveling_wizard import refresh_posted_wizard
        await refresh_posted_wizard(interaction.client, self.guild_id, clone_id=self.clone_id)


class LevelingAddRewardButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("add")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="➕ Add reward", style=discord.ButtonStyle.success,
            custom_id=_encode("add", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.send_modal(LevelingRewardLevelModal(self.guild_id, self.clone_id, self.invoker_id))


DYNAMIC_ITEMS = (
    LevelingXpRateSelect, LevelingAnnounceChannelSelect, LevelingCardStyleSelect,
    LevelingAddRewardButton, LevelingRewardRoleSelect,
)
