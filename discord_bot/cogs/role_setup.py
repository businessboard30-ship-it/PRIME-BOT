# path: discord_bot/cogs/role_setup.py

"""
/role setup — bulk-create common self-assignable roles and post a
member-facing button panel for them, in one wizard.

This is deliberately NOT a new subsystem: it's a front door onto the
existing reaction_roles infra (discord_reaction_roles table +
ReactionRoleView/ReactionRoleButton in reaction_roles.py). Reasons:
- No new table/migration needed, so nothing to keep in sync with the
  guild/clone scoping rules that table already follows.
- The panel this wizard posts is rebuilt on restart by
  ReactionRolesCog.cog_load exactly like a panel made via
  /reactionrole create+add — one persistence code path, not two.
- Admins keep using /reactionrole add|remove|list to hand-edit whatever
  this wizard produced, instead of a second, parallel management
  surface with its own drift risk.

Command surface is a single group, `/role setup`, chosen over a bare
`/role` because `/role` alone can't take a description while leaving
room to add sibling subcommands later (e.g. a future `/role panel`)
without a breaking rename.
"""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs.reaction_roles import ReactionRoleView, MAX_ROLES_PER_PANEL
from database import db

logger = logging.getLogger(__name__)

# key, display label, button emoji, embed/role color (hex). Kept short
# enough that create-all + a channel picker + finish/cancel all fit in
# one view (17 options + 4 control rows well under discord.ui's 25-item /
# 5-row cap since the presets live in a single multi-select, not one
# button each).
PRESET_ROLES = [
    ("announcements", "Announcements", "📢", 0xED4245),
    ("updates", "Updates", "🔔", 0x3498DB),
    ("giveaways", "Giveaways", "🎁", 0xF1C40F),
    ("events", "Events", "📅", 0x9B59B6),
    ("polls", "Polls", "📊", 0x1ABC9C),
    ("music", "Music", "🎵", 0xEB459E),
    ("movie_night", "Movie Night", "🎬", 0x992D22),
    ("game_night", "Game Night", "🎮", 0x2ECC71),
    ("lfg", "LFG", "🎯", 0xE67E22),
    ("server_news", "Server News", "📰", 0x5865F2),
    ("suggestions", "Suggestions", "💡", 0xFEE75C),
    ("birthday_pings", "Birthday Pings", "🎂", 0xEE79C2),
    ("contests", "Contests", "🏆", 0xC9A227),
    ("community_calls", "Community Calls", "🗣️", 0x11A8CD),
    ("patch_notes", "Patch Notes", "🛠️", 0x99AAB5),
    ("trivia_night", "Trivia Night", "🧠", 0x4B0082),
    ("feedback_pings", "Feedback Pings", "📝", 0x117864),
]
PRESET_BY_KEY = {p[0]: p for p in PRESET_ROLES}
DEFAULT_PANEL_CHANNEL_NAME = "roles"


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as verification.py/reaction_roles.py: None on the
    main bot, the clone's row id on a clone process."""
    return getattr(interaction.client, "clone_id", None)


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


class PresetSelect(discord.ui.Select):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                emoji=emoji,
                default=key in wizard.created,  # pre-tick ones already made
            )
            for key, label, emoji, _color in PRESET_ROLES
        ]
        super().__init__(
            placeholder="Pick roles to create (or use Create All)",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.selected = set(self.values)
        await self.wizard.refresh(interaction)


class CreateSelectedButton(discord.ui.Button):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        super().__init__(label="Create selected", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not self.wizard.selected:
            await interaction.response.send_message(
                "Pick at least one role from the dropdown first, or use **Create all 17**.", ephemeral=True
            )
            return
        await self.wizard.create_roles(interaction, self.wizard.selected)


class CreateAllButton(discord.ui.Button):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        super().__init__(label="Create all 17", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        await self.wizard.create_roles(interaction, [p[0] for p in PRESET_ROLES])


class PanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        super().__init__(
            placeholder="Choose the channel for the role panel (optional — defaults to #roles)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.channel_id = self.values[0].id if self.values else None
        await self.wizard.refresh(interaction)


class FinishButton(discord.ui.Button):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        super().__init__(label="Post role panel", style=discord.ButtonStyle.success, emoji="📌", row=3)

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if not wizard.created:
            await interaction.response.send_message(
                "Create at least one role first, then post the panel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        channel: Optional[discord.TextChannel] = None
        if wizard.channel_id:
            channel = guild.get_channel(wizard.channel_id)
            if channel is None:
                await interaction.followup.send("That channel no longer exists — pick another.", ephemeral=True)
                return
        else:
            channel = discord.utils.find(
                lambda c: c.name == DEFAULT_PANEL_CHANNEL_NAME, guild.text_channels
            )
            if channel is None:
                try:
                    channel = await guild.create_text_channel(
                        DEFAULT_PANEL_CHANNEL_NAME,
                        reason=f"Self-roles panel auto-created by {interaction.user} via /role setup",
                    )
                except discord.Forbidden:
                    await interaction.followup.send(
                        "I don't have permission to create a #roles channel — grant me **Manage Channels**, "
                        "or pick an existing channel in the dropdown above and try again.",
                        ephemeral=True,
                    )
                    return
                except discord.HTTPException as e:
                    await interaction.followup.send(f"Couldn't create #roles: {e}", ephemeral=True)
                    return

        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                f"I need **Send Messages** and **Embed Links** in {channel.mention} to post the panel there — "
                "fix permissions or pick a different channel.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎭 Self-roles",
            description="Tap a button below to give yourself a role — tap it again to remove it. Pick as many as you like.",
            color=discord.Color.blurple(),
        )
        panel_roles = [
            {"role_id": rid, "label": PRESET_BY_KEY[key][1], "emoji": PRESET_BY_KEY[key][2]}
            for key, rid in wizard.created.items()
        ][:MAX_ROLES_PER_PANEL]
        view = ReactionRoleView(panel_roles)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(f"I couldn't post in {channel.mention} — check my permissions there.", ephemeral=True)
            return

        clone_id = _clone_id_of(interaction)
        for r in panel_roles:
            ok = await db.add_reaction_role(
                guild.id, channel.id, message.id, r["role_id"], r["label"], r["emoji"],
                interaction.user.id, clone_id=clone_id,
            )
            if not ok:
                logger.warning("role_setup: failed saving reaction role %s for panel %s", r["role_id"], message.id)

        interaction.client.add_view(view, message_id=message.id)

        await interaction.followup.send(
            f"✅ Posted the role panel in {channel.mention} with {len(panel_roles)} role(s). "
            "Use `/reactionrole add` or `/reactionrole remove` any time to hand-edit it, or run "
            "`/role setup` again to create more preset roles and post a fresh panel.",
            ephemeral=True,
        )
        wizard.stop()


class CancelButton(discord.ui.Button):
    def __init__(self, wizard: "RoleSetupWizard"):
        self.wizard = wizard
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.stop()
        await interaction.response.edit_message(content="Role setup cancelled — any roles already created were left in place.", embed=None, view=None)


class RoleSetupWizard(discord.ui.View):
    def __init__(self, invoker_id: int, guild: Optional[discord.Guild] = None):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.selected: set = set()
        self.created: dict = {}  # preset_key -> role_id
        self.channel_id: Optional[int] = None
        self._busy = False

        # Detect presets that already exist in the guild (e.g. from a run
        # before a restart) so the wizard opens showing accurate state
        # instead of everything unchecked.
        if guild is not None:
            by_name = {r.name.casefold(): r for r in guild.roles}
            for key, label, _emoji, _color in PRESET_ROLES:
                existing = by_name.get(label.casefold())
                if existing is not None:
                    self.created[key] = existing.id

        self.add_item(PresetSelect(self))
        self.add_item(CreateSelectedButton(self))
        self.add_item(CreateAllButton(self))
        self.add_item(PanelChannelSelect(self))
        self.add_item(FinishButton(self))
        self.add_item(CancelButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran /role setup can use this.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        lines = []
        for key, label, emoji, _color in PRESET_ROLES:
            mark = "✅" if key in self.created else "⬜"
            lines.append(f"{mark} {emoji} {label}")
        embed = discord.Embed(
            title="Self-role setup wizard",
            description=(
                "Pick roles from the dropdown and tap **Create selected**, or tap **Create all 17** "
                "to make every preset in one go. Already-existing roles with the same name are reused, "
                "not duplicated. Then choose a channel (or leave it blank for an auto-created #roles) "
                "and hit **Post role panel** to let members self-assign."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name=f"Presets ({len(self.created)}/{len(PRESET_ROLES)} created)", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Panel channel",
            value=(f"<#{self.channel_id}>" if self.channel_id else f"*not set — will use/create #{DEFAULT_PANEL_CHANNEL_NAME}*"),
            inline=False,
        )
        return embed

    async def refresh(self, interaction: discord.Interaction):
        # Rebuild the select's `default` flags to reflect self.created/self.selected.
        for item in self.children:
            if isinstance(item, PresetSelect):
                for opt in item.options:
                    opt.default = opt.value in self.selected
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def _set_buttons_disabled(self, disabled: bool):
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = disabled

    async def create_roles(self, interaction: discord.Interaction, keys):
        if self._busy:
            await interaction.response.send_message("Already creating roles — one sec.", ephemeral=True)
            return
        self._busy = True

        # Dim every control immediately so a second tap has nothing live to
        # hit, and edit the message right away so the user sees it's working
        # rather than appearing to hang.
        self._set_buttons_disabled(True)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        progress = await interaction.followup.send(
            f"⏳ Creating {len(keys)} role(s)... this can take a few seconds.", ephemeral=True
        )

        guild = interaction.guild
        created_now = []
        reused = []
        failed = []

        for key in keys:
            if key in self.created:
                continue  # already handled in an earlier click this session
            _key, label, _emoji, color = PRESET_BY_KEY[key]

            existing = discord.utils.find(lambda r, l=label: r.name.casefold() == l.casefold(), guild.roles)
            if existing is not None:
                self.created[key] = existing.id
                reused.append(label)
                continue

            try:
                role = await guild.create_role(
                    name=label,
                    color=discord.Color(color),
                    permissions=discord.Permissions.none(),
                    mentionable=True,
                    reason=f"Self-role preset created by {interaction.user} via /role setup",
                )
            except discord.Forbidden:
                failed.append(label)
                continue
            except discord.HTTPException as e:
                logger.warning("role_setup: failed creating role %s in guild %s: %s", label, guild.id, e)
                failed.append(label)
                continue

            self.created[key] = role.id
            created_now.append(label)

        self._busy = False
        self.selected = set()
        self._set_buttons_disabled(False)
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

        if created_now or reused or failed:
            parts = ["✅ Done."]
            if created_now:
                parts.append(f"✅ Created: {', '.join(created_now)}")
            if reused:
                parts.append(f"♻️ Already existed, reused (no duplicates): {', '.join(reused)}")
            if failed:
                parts.append(f"⚠️ Couldn't create (check my **Manage Roles** permission/hierarchy): {', '.join(failed)}")
        else:
            parts = ["Nothing to do — all selected roles were already created in this session."]
        await progress.edit(content="\n".join(parts))


class RoleSetupCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.guild_only()(app_commands.Group(name="role", description="Self-assignable role tools"))

    @group.command(name="setup", description="Wizard: bulk-create common self-roles and post a member panel")
    @app_commands.default_permissions(manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction):
        if not _require_perm(interaction, "manage_roles"):
            await interaction.response.send_message("You need the **Manage Roles** permission to do that.", ephemeral=True)
            return
        if interaction.guild.me.guild_permissions.manage_roles is False:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission myself before I can create or assign roles here.",
                ephemeral=True,
            )
            return
        wizard = RoleSetupWizard(interaction.user.id, guild=interaction.guild)
        await interaction.response.send_message(embed=wizard.build_embed(), view=wizard, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleSetupCog(bot))
