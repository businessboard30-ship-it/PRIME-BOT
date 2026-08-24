# path: discord_bot/cogs/_views_economy_wizard.py

"""
Bumper-style setup wizard for /ecoconfig setup. Same DynamicItem/
restart-proof pattern as the other three wizards.

Deliberately does NOT include a "shop tax" step: the ecoconfig_wizard.html
mockup has one, but no shop_tax column (or any tax-on-purchase logic)
exists anywhere in database.py or economy.py's /buy flow. Adding a UI
control for a setting nothing reads would be worse than no control at
all — it would look configured and silently do nothing. Steps here are
scoped to fields _ECONOMY_CONFIG_DEFAULTS actually has: currency name/
symbol, daily amount, work range, rob success odds. (Vote-bonus and
ad-bonus config already have their own commands — /ecoconfig vote,
/ecoconfig ads — and are left there rather than folded into this wizard,
since each needs 3-4 free-text fields a modal can't comfortably hold
alongside everything else on one message.)
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

ROB_ODDS_CHOICES = [10, 25, 40, 55, 70]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def render_status_lines(config: dict) -> list:
    name = config.get("currency_name", "Coins")
    symbol = config.get("currency_symbol", "🪙")
    daily = config.get("daily_amount", 100)
    work_min = config.get("work_min", 20)
    work_max = config.get("work_max", 80)
    rob_odds = config.get("rob_success_chance", 40)

    return [
        f"✅ **Step 1: Currency** — {symbol} {name}",
        f"✅ **Step 2: Daily / work payouts** — daily {daily}, work {work_min}–{work_max}",
        f"✅ **Step 3: Rob success odds** — {rob_odds}%",
    ]


def _id_pattern(field: str) -> str:
    return rf"^ecowz_{field}:(\d+):(-|\d+):(-|\d+)$"


def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"ecowz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "ecoconfig", "manage_guild", "Manage Server")


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.gold())

    edit_row = discord.ui.ActionRow()
    edit_row.add_item(EconomyCurrencyButton(guild_id, clone_id, invoker_id))
    edit_row.add_item(EconomyPayoutsButton(guild_id, clone_id, invoker_id))
    rob_row = discord.ui.ActionRow()
    rob_row.add_item(EconomyRobOddsSelect(guild_id, clone_id, invoker_id, config))

    text = discord.ui.TextDisplay("\n".join(["### 💰 Set up economy", *render_status_lines(config)]))
    for item in (text, discord.ui.Separator(), edit_row, rob_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    # is_done() guard: some callers (e.g. toggle/action buttons that do
    # async work before this) already defer()/respond before calling in —
    # calling response.defer() again would raise InteractionResponded.
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_economy_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_economy_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    config = await db.get_economy_config(guild_id, clone_id=clone_id)
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


class EconomyCurrencyModal(discord.ui.Modal, title="Currency name & symbol"):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.name = discord.ui.TextInput(label="Currency name", default=config.get("currency_name", "Coins"), max_length=32)
        self.symbol = discord.ui.TextInput(label="Currency symbol (emoji or short text)", default=config.get("currency_symbol", "🪙"), max_length=8)
        self.add_item(self.name)
        self.add_item(self.symbol)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.set_economy_config(
            self.guild_id, clone_id=self.clone_id,
            currency_name=str(self.name.value), currency_symbol=str(self.symbol.value),
        )
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        view = build_wizard_view(self.guild_id, self.clone_id, self.invoker_id, config)
        await interaction.edit_original_response(view=view)


class EconomyPayoutsModal(discord.ui.Modal, title="Daily / work payout ranges"):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.daily = discord.ui.TextInput(label="Daily amount", default=str(config.get("daily_amount", 100)), max_length=10)
        self.work_min = discord.ui.TextInput(label="Work minimum", default=str(config.get("work_min", 20)), max_length=10)
        self.work_max = discord.ui.TextInput(label="Work maximum", default=str(config.get("work_max", 80)), max_length=10)
        self.add_item(self.daily)
        self.add_item(self.work_min)
        self.add_item(self.work_max)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            daily = int(str(self.daily.value))
            work_min = int(str(self.work_min.value))
            work_max = int(str(self.work_max.value))
        except ValueError:
            await interaction.followup.send("Those need to be whole numbers.", ephemeral=True)
            return
        if work_min > work_max:
            work_min, work_max = work_max, work_min
        await db.set_economy_config(
            self.guild_id, clone_id=self.clone_id,
            daily_amount=daily, work_min=work_min, work_max=work_max,
        )
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        view = build_wizard_view(self.guild_id, self.clone_id, self.invoker_id, config)
        await interaction.edit_original_response(view=view)


class EconomyCurrencyButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("currency")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Currency name and symbol", style=discord.ButtonStyle.secondary,
            custom_id=_encode("currency", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        await interaction.response.send_modal(EconomyCurrencyModal(self.guild_id, self.clone_id, self.invoker_id, config))


class EconomyPayoutsButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("payouts")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Daily and work payout ranges", style=discord.ButtonStyle.secondary,
            custom_id=_encode("payouts", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        await interaction.response.send_modal(EconomyPayoutsModal(self.guild_id, self.clone_id, self.invoker_id, config))


class EconomyRobOddsSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("rob")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("rob_success_chance", 40)
        options = [
            discord.SelectOption(label=f"{n}%", value=str(n), default=(n == current))
            for n in ROB_ODDS_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 3 — rob success odds", options=options,
            custom_id=_encode("rob", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_economy_config(self.guild_id, clone_id=self.clone_id, rob_success_chance=int(self.item.values[0]))
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


DYNAMIC_ITEMS = (EconomyCurrencyButton, EconomyPayoutsButton, EconomyRobOddsSelect)
