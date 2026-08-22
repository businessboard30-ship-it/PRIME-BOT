# path: discord_bot/cogs/_views_ticket_wizard.py

"""
Bumper-style multi-step setup wizard for /ticket setup.

Same pattern as _views_welcome.py and _views_automod_wizard.py — one
message, a checklist that fills in with ✅ as each step is completed,
built entirely from discord.ui.DynamicItem so it's timeout=None and
restart-proof (guild_id / clone_id / invoker_id encoded straight into
each component's custom_id, reconstructed on the fly by from_custom_id;
registered once via bot.add_dynamic_items(*DYNAMIC_ITEMS) in
discord_bot/bot.py's setup_hook).

Differs from the other two wizards in one way worth calling out:
picking Step 1's panel channel does NOT immediately post the panel (the
welcome/automod wizards' selects write straight to config on every
change). Posting a support panel is a visible, guild-wide action, so it
stays behind its own explicit "Post panel now" button — same reasoning
as WelcomeToggleButton requiring a channel first, just applied to a
bigger side effect.
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

MAX_MESSAGE_LEN = 300


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _status_color(config: dict) -> discord.Color:
    return discord.Color.green() if config.get("panel_message_id") else discord.Color.blurple()


def render_status_lines(config: dict) -> list:
    panel_ch = config.get("panel_channel_id")
    role_id = config.get("support_role_id")
    cat_id = config.get("category_id")
    msg = config.get("welcome_message") or ""
    msg_preview = (msg[:80] + "…") if len(msg) > 80 else msg
    posted = bool(config.get("panel_message_id"))

    step1 = "✅" if panel_ch else "⬜"
    step2 = "✅" if role_id else "⬜"
    step3 = "✅" if cat_id else "⬜"

    return [
        f"{step1} **Step 1: Panel channel** — {f'<#{panel_ch}>' if panel_ch else '*not set*'}",
        f"{step2} **Step 2: Support role** — {f'<@&{role_id}>' if role_id else '*not set*'}",
        f"{step3} **Step 3: Ticket category** — {f'<#{cat_id}>' if cat_id else '*not set*'}",
        f"✅ **Step 4: Welcome message** — {msg_preview or '*default*'}",
        f"-# Panel status: {'posted — <#' + str(panel_ch) + '>' if posted else 'not posted yet'}",
    ]


# ---------------------------------------------------------------------------
# custom_id shape: ticketwz_<field>:<guild_id>:<clone_id or "-">:<invoker_id or "-">
# ---------------------------------------------------------------------------

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"ticketwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^ticketwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "ticket", "manage_guild", "Manage Server")


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(config))

    chan_row = discord.ui.ActionRow()
    chan_row.add_item(TicketPanelChannelSelect(guild_id, clone_id, invoker_id, config))
    role_row = discord.ui.ActionRow()
    role_row.add_item(TicketSupportRoleSelect(guild_id, clone_id, invoker_id, config))
    cat_row = discord.ui.ActionRow()
    cat_row.add_item(TicketCategorySelect(guild_id, clone_id, invoker_id, config))

    button_row = discord.ui.ActionRow()
    button_row.add_item(TicketEditMessageButton(guild_id, clone_id, invoker_id))
    button_row.add_item(TicketPostPanelButton(guild_id, clone_id, invoker_id))

    text = discord.ui.TextDisplay("\n".join(["### 🎫 Set up tickets", *render_status_lines(config)]))
    for item in (text, discord.ui.Separator(), chan_row, role_row, cat_row, discord.ui.Separator(), button_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    # is_done() guard: some callers (e.g. toggle/action buttons that do
    # async work before this) already defer()/respond before calling in —
    # calling response.defer() again would raise InteractionResponded.
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_ticket_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_ticket_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Best-effort, silent — mirrors _views_automod_wizard.py's version.
    Nothing in ticket.py currently writes config outside the wizard, but
    this exists for the same reason theirs does: any future standalone
    /ticket command (or another surface) that writes config should call
    this afterward so an open wizard doesn't go stale."""
    config = await db.get_ticket_config(guild_id, clone_id=clone_id)
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


class TicketMessageModal(discord.ui.Modal, title="Ticket welcome message"):
    def __init__(self, guild_id: int, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.template = discord.ui.TextInput(
            label="Message shown when a ticket opens",
            style=discord.TextStyle.paragraph,
            default=current or "",
            max_length=MAX_MESSAGE_LEN,
            required=False,
        )
        self.add_item(self.template)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.set_ticket_config(self.guild_id, clone_id=self.clone_id, welcome_message=str(self.template.value) or None)
        config = await db.get_ticket_config(self.guild_id, clone_id=self.clone_id)
        view = build_wizard_view(self.guild_id, self.clone_id, self.invoker_id, config)
        await interaction.edit_original_response(view=view)


class TicketPanelChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("chan")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 1 — pick the panel channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("chan", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        await db.set_ticket_config(self.guild_id, clone_id=self.clone_id, panel_channel_id=channel.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class TicketSupportRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_id_pattern("role")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="Step 2 — pick the support role",
            min_values=1, max_values=1,
            custom_id=_encode("role", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        role = self.item.values[0]
        await db.set_ticket_config(self.guild_id, clone_id=self.clone_id, support_role_id=role.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class TicketCategorySelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("cat")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 3 — pick the ticket category",
            channel_types=[discord.ChannelType.category],
            min_values=1, max_values=1,
            custom_id=_encode("cat", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        category = self.item.values[0]
        await db.set_ticket_config(self.guild_id, clone_id=self.clone_id, category_id=category.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class TicketEditMessageButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("edit")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="💬 Edit message", style=discord.ButtonStyle.secondary,
            custom_id=_encode("edit", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_ticket_config(self.guild_id, clone_id=self.clone_id)
        await interaction.response.send_modal(
            TicketMessageModal(self.guild_id, self.clone_id, self.invoker_id, config.get("welcome_message"))
        )


class TicketPostPanelButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("post")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="📮 Post panel now", style=discord.ButtonStyle.success,
            custom_id=_encode("post", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Imported here (not at module scope) to avoid a cross-cog import
        # cycle: ticket.py will import this module to launch the wizard.
        from discord_bot.cogs.ticket import TicketPanelView

        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_ticket_config(self.guild_id, clone_id=self.clone_id)
        channel_id = config.get("panel_channel_id")
        if not channel_id:
            await interaction.followup.send("Pick a panel channel (Step 1) first.", ephemeral=True)
            return
        channel = interaction.client.get_channel(int(channel_id))
        if channel is None:
            await interaction.followup.send("Couldn't find that channel — pick it again in Step 1.", ephemeral=True)
            return

        cog = interaction.client.get_cog("TicketCog")
        panel_template = config.get("welcome_message") or "Click below to open a private ticket with our support team."
        panel_description = panel_template.replace("{member}", "there").replace("{guild}", channel.guild.name)
        embed = discord.Embed(
            title="🎫 Need help?",
            description=panel_description,
            color=discord.Color.blurple(),
        )
        try:
            panel_message = await channel.send(embed=embed, view=TicketPanelView(cog))
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"Couldn't post the panel there: {e}", ephemeral=True)
            return

        await db.set_ticket_config(self.guild_id, clone_id=self.clone_id, panel_message_id=panel_message.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


DYNAMIC_ITEMS = (
    TicketPanelChannelSelect, TicketSupportRoleSelect, TicketCategorySelect,
    TicketEditMessageButton, TicketPostPanelButton,
)
