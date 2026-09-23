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
symbol, daily amount, work range, rob success odds. Step 4 is the
server's sponsor ad (the /watchad bonus): an edit modal that holds all five
ad_* fields plus an Activate/Deactivate button, so nothing has to be typed
as a slash command. /ecoconfig ads still works and writes the same columns.
(Vote-bonus config keeps its own command, /ecoconfig vote — it needs 3-4
free-text fields a modal can't comfortably hold alongside everything else.)
"""

import re

import discord
from discord_bot import perm_check

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

ROB_ODDS_CHOICES = [10, 25, 40, 55, 70]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _ad_status_line(config: dict) -> str:
    enabled = bool(config.get("ad_bonus_enabled", False))
    title = config.get("ad_embed_title") or "Sponsored"
    symbol = config.get("currency_symbol", "🪙")
    amount = config.get("ad_bonus_amount", 50)
    cooldown = config.get("ad_cooldown_hours", 4)
    if enabled:
        return f"✅ **Step 4: Sponsor ad** — 🟢 live: “{title}” · +{amount} {symbol} every {cooldown}h"
    return f"⬜ **Step 4: Sponsor ad** — 🔴 off (“{title}” · +{amount} {symbol} every {cooldown}h when on)"


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
        _ad_status_line(config),
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
    ad_row = discord.ui.ActionRow()
    ad_row.add_item(EconomyAdEditButton(guild_id, clone_id, invoker_id))
    ad_row.add_item(EconomyAdToggleButton(guild_id, clone_id, invoker_id, bool(config.get("ad_bonus_enabled", False))))

    text = discord.ui.TextDisplay("\n".join(["### 💰 Set up economy", *perm_check.lines(guild_id, clone_id), *render_status_lines(config)]))
    for item in (text, discord.ui.Separator(), edit_row, rob_row, ad_row):
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


class EconomyAdModal(discord.ui.Modal, title="Sponsor ad"):
    """All five ad_* settings in one modal (Discord's 5-field max). Saving does
    NOT flip it live — the Activate button next to it does, so an admin can
    draft an ad without it going out half-written."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.ad_title = discord.ui.TextInput(
            label="Headline", default=config.get("ad_embed_title") or "", required=False, max_length=100)
        self.ad_description = discord.ui.TextInput(
            label="Message", style=discord.TextStyle.paragraph,
            default=config.get("ad_embed_description") or "", required=False, max_length=500)
        self.ad_url = discord.ui.TextInput(
            label="Link (optional, https://…)", default=config.get("ad_embed_url") or "",
            required=False, max_length=300)
        self.ad_amount = discord.ui.TextInput(
            label="Bonus amount per claim", default=str(config.get("ad_bonus_amount", 50)), max_length=10)
        self.ad_cooldown = discord.ui.TextInput(
            label="Cooldown (hours)", default=str(config.get("ad_cooldown_hours", 4)), max_length=4)
        for item in (self.ad_title, self.ad_description, self.ad_url, self.ad_amount, self.ad_cooldown):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            amount = int(str(self.ad_amount.value).strip())
            cooldown = int(str(self.ad_cooldown.value).strip())
        except ValueError:
            await interaction.followup.send("Bonus amount and cooldown need to be whole numbers.", ephemeral=True)
            return
        if amount < 0 or cooldown < 1:
            await interaction.followup.send("Bonus can't be negative and cooldown must be at least 1 hour.", ephemeral=True)
            return
        url = str(self.ad_url.value).strip()
        if url and not re.match(r"^https?://\S+$", url):
            await interaction.followup.send("The link needs to start with http:// or https:// (or leave it empty).", ephemeral=True)
            return
        await db.set_economy_config(
            self.guild_id, clone_id=self.clone_id,
            ad_embed_title=str(self.ad_title.value).strip() or None,
            ad_embed_description=str(self.ad_description.value).strip() or None,
            ad_embed_url=url or None,
            ad_bonus_amount=amount, ad_cooldown_hours=cooldown,
        )
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        view = build_wizard_view(self.guild_id, self.clone_id, self.invoker_id, config)
        await interaction.edit_original_response(view=view)


class EconomyAdEditButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("adedit")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Edit sponsor ad", style=discord.ButtonStyle.secondary,
            custom_id=_encode("adedit", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        await interaction.response.send_modal(EconomyAdModal(self.guild_id, self.clone_id, self.invoker_id, config))


class EconomyAdToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("adtoggle")):
    """Activate / Deactivate the sponsor ad. The custom_id deliberately carries
    no on/off state — the callback reads the live value and flips it, so a
    stale wizard message (or two admins clicking) can never set the wrong state."""

    def __init__(self, guild_id: int, clone_id, invoker_id, enabled: bool = False):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="⏸️ Deactivate ad" if enabled else "▶️ Activate ad",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=_encode("adtoggle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_economy_config(self.guild_id, clone_id=self.clone_id)
        await db.set_economy_config(
            self.guild_id, clone_id=self.clone_id,
            ad_bonus_enabled=not bool(config.get("ad_bonus_enabled", False)),
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


DYNAMIC_ITEMS = (EconomyCurrencyButton, EconomyPayoutsButton, EconomyRobOddsSelect,
                 EconomyAdEditButton, EconomyAdToggleButton)
