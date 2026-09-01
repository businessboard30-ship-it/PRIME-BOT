# path: discord_bot/cogs/_views_roast_arena_host_wizard.py

"""
Apply-to-host wizard for the roast arena's single shared battleground (see
discord_bot/cogs/roast_arena.py). The arena has exactly ONE active host at a
time — clones can still raise and organize battles, but every battle
resolves to whichever guild/channel is currently approved here. Nothing in
this file lets a server set its own battleground; it only lets a server
*apply*, and only an owner/main-bot admin (DISCORD_CLONE_ADMIN_IDS) can
approve one.

Same LayoutView + persistent DynamicItem/regex custom_id pattern as the other
wizards (_views_economy_wizard.py etc.), so buttons survive a restart.

Flow:
  1. /roastarena apply (any admin, run in the channel they want to offer)
     → build_apply_wizard_view() renders current host status + an Apply
       button. Clicking it upserts a 'pending' row in
       discord_roast_arena_host_requests (one live pending row per guild —
       re-applying just refreshes it) and re-renders the wizard in place.
  2. Every DISCORD_CLONE_ADMIN_IDS owner gets DMed an approve/deny card
     (build_review_view) via RoastArenaCog._dm_admins-style delivery, wired
     in roast_arena.py's on_host_apply / on_host_approve / on_host_deny.
  3. Approve → db.set_roast_arena_host(...) flips the singleton, every other
     pending request is auto-superseded (one host at a time), and the
     applying server's wizard message (if still open) is refreshed to show
     the new status.
"""

import re

import discord

from discord_bot.cogs._views_shared import check_wizard_access

_COG_NAME = "RoastArenaCog"


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog(_COG_NAME)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


# ─────────────────────────────────────────────────────────────────────────
# Wizard card (posted by /roastarena apply)
# ─────────────────────────────────────────────────────────────────────────
def render_status_lines(host: dict, pending: "dict | None", *, this_guild_id: int) -> list:
    if host.get("guild_id") == this_guild_id:
        lines = ["🏆 **This server is the current battleground.**"]
    elif host.get("guild_id"):
        lines = ["📍 The battleground is currently hosted elsewhere."]
    else:
        lines = ["📍 No server has been approved yet — battles default to the support server."]

    if pending:
        lines.append(f"⏳ **Application pending** for <#{pending['channel_id']}> — awaiting review.")
    return lines


def build_apply_wizard_view(
    guild_id: int, invoker_id: int, channel_id: int, host: dict, pending: "dict | None"
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.orange())

    text = discord.ui.TextDisplay(
        "\n".join(
            [
                "### 🏟️ Apply to host the roast arena",
                f"Applying will offer <#{channel_id}> as the battleground.",
                *render_status_lines(host, pending, this_guild_id=guild_id),
            ]
        )
    )
    row = discord.ui.ActionRow()
    row.add_item(HostApplyButton(guild_id, invoker_id, channel_id, disabled=bool(pending)))
    for item in (text, discord.ui.Separator(), row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, invoker_id: int, channel_id: int):
    if not interaction.response.is_done():
        await interaction.response.defer()
    cog = _cog(interaction)
    host = await cog.get_arena_host()
    pending = await cog.get_pending_host_request(guild_id)
    view = build_apply_wizard_view(guild_id, invoker_id, channel_id, host, pending)
    await interaction.edit_original_response(view=view)


def _id_pattern(field: str) -> str:
    return rf"hostwz_{field}:(\d+):(\d+):(\d+)$"


def _encode(field: str, guild_id: int, invoker_id: int, channel_id: int) -> str:
    return f"hostwz_{field}:{guild_id}:{invoker_id}:{channel_id}"


class HostApplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_id_pattern("apply"),
):
    def __init__(self, guild_id: int, invoker_id: int, channel_id: int, *, disabled: bool = False):
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.channel_id = channel_id
        super().__init__(
            discord.ui.Button(
                label="Apply to host here" if not disabled else "Application pending",
                style=discord.ButtonStyle.success,
                emoji="🏟️",
                disabled=disabled,
                custom_id=_encode("apply", guild_id, invoker_id, channel_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        guild_id, invoker_id, channel_id = (int(match.group(i)) for i in (1, 2, 3))
        return cls(guild_id, invoker_id, channel_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await check_wizard_access(interaction, self.invoker_id, "roastarena apply", "manage_guild", "Manage Server"):
            return
        cog = _cog(interaction)
        if cog is None:
            await interaction.followup.send("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_host_apply(interaction, self.guild_id, self.channel_id, interaction.user.id)
        await _rerender(interaction, self.guild_id, self.invoker_id, self.channel_id)


# ─────────────────────────────────────────────────────────────────────────
# Review card (DMed to owner / DISCORD_CLONE_ADMIN_IDS)
# ─────────────────────────────────────────────────────────────────────────
def build_review_embed(guild_name: str, channel_mention: str, applicant: "discord.abc.User") -> discord.Embed:
    embed = discord.Embed(
        title="🏟️ New battleground application",
        description=f"**{guild_name}** is offering {channel_mention} as the shared roast-arena battleground.",
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"Applied by {applicant} ({applicant.id})")
    return embed


def build_review_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HostApproveButton(request_id))
    view.add_item(HostDenyButton(request_id))
    return view


class HostApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hostreview:approve:(?P<request_id>\d+)",
):
    def __init__(self, request_id: int):
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="approve", style=discord.ButtonStyle.success, emoji="✅",
                custom_id=f"hostreview:approve:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["request_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_host_review(interaction, self.request_id, approve=True)


class HostDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hostreview:deny:(?P<request_id>\d+)",
):
    def __init__(self, request_id: int):
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="deny", style=discord.ButtonStyle.danger, emoji="✋",
                custom_id=f"hostreview:deny:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["request_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_host_review(interaction, self.request_id, approve=False)


DYNAMIC_ITEMS = [HostApplyButton, HostApproveButton, HostDenyButton]
