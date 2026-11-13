# path: discord_bot/cogs/_views_role_helper.py

"""Persistent "Create this role for me" buttons (survive restarts: DynamicItems whose custom_id carries kind/server/clone).

  rolehelper:create:<kind>:<guild>:<clone>   on the reminder message: creates the role(s), then the reminder turns into the result
  rolehelper:make:<kind>:<guild>:<clone>     inside a setup screen: creates the role(s) and answers privately; the screen is left alone
  rolehelper:later:<kind>:<guild>:<clone>    on the reminder message: "not now" (the reminder is never sent again)

Needs Manage Server, same as the screens themselves.
"""
from __future__ import annotations

import logging
import re

import discord

from modules import role_helper as rh

logger = logging.getLogger(__name__)

ACCENT = discord.Color.from_rgb(88, 101, 242)
_KINDS = "|".join(rh.KINDS)
_PATTERN = rf"^rolehelper:(create|make|later):({_KINDS}):(\d+):(-|\d+)$"
LABELS = {"ticket": "Create support role", "welcome": "Create member & bot roles",
          "raid": "Create alert role", "honey": "Create alert role"}


def _cid(action: str, kind: str, guild_id: int, clone_id) -> str:
    return f"rolehelper:{action}:{kind}:{int(guild_id)}:{'-' if clone_id is None else int(clone_id)}"


def result_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(text))
    view.add_item(box)
    return view


class RoleHelperButton(discord.ui.DynamicItem[discord.ui.Button], template=_PATTERN):
    def __init__(self, action: str, kind: str, guild_id: int, clone_id):
        self.action, self.kind, self.guild_id, self.clone_id = action, kind, int(guild_id), clone_id
        if action == "later":
            label, style, emoji = "Not now", discord.ButtonStyle.secondary, None
        else:
            label, style, emoji = LABELS[kind], discord.ButtonStyle.success, "🛠️"
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=_cid(action, kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        cid = match.group(4)
        return cls(match.group(1), match.group(2), int(match.group(3)), None if cid == "-" else int(cid))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        perms = getattr(interaction.user, "guild_permissions", None)
        if guild is None or guild.id != self.guild_id or not (perms and (perms.manage_guild or perms.administrator)):
            await interaction.response.send_message("🛠️ Only people with **Manage Server** can use this.", ephemeral=True)
            return
        try:
            if self.action == "later":
                await rh.mark(guild.id, self.clone_id, self.kind, "later")
                await interaction.response.edit_message(view=result_view(
                    "👍 No problem. I won't remind you again. You can pick or create the role any time in the setup screen."))
                return
            await interaction.response.defer(ephemeral=True)
            res = await rh.create_missing(self.kind, guild, self.clone_id, interaction.user.id)
            if self.action == "create" and (res.ok or res.already):
                await rh.mark(guild.id, self.clone_id, self.kind, "done")
                try:
                    await interaction.message.edit(view=result_view(res.text(self.kind)),
                                                   allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    logger.debug("couldn't update the reminder message", exc_info=True)
            await interaction.followup.send(res.text(self.kind), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[role-helper] button failed guild=%s kind=%s", self.guild_id, self.kind)
            if interaction.response.is_done():
                await interaction.followup.send("😿 Something went wrong. Try again in a moment.", ephemeral=True)
            else:
                await interaction.response.send_message("😿 Something went wrong. Try again in a moment.", ephemeral=True)


def reminder_view(kind: str, guild_id: int, clone_id) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(rh.NUDGE_TEXT[kind]))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(RoleHelperButton("create", kind, guild_id, clone_id),
                                      RoleHelperButton("later", kind, guild_id, clone_id)))
    view.add_item(box)
    return view


def make_button(kind: str, guild_id: int, clone_id) -> RoleHelperButton:
    """The in-screen button (answers privately, never edits the setup screen)."""
    return RoleHelperButton("make", kind, guild_id, clone_id)


DYNAMIC_ITEMS = (RoleHelperButton,)
