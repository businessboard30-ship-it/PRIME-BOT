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
from modules.text_styles import plain_name as _plain_name
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs import _ai_bulk_ops
from modules import ai_bulk as _ai_bulk
from modules import moderation_extra as modx
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
                lambda c: _plain_name(c.name) == DEFAULT_PANEL_CHANNEL_NAME, guild.text_channels
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


    # ── /role create · /role bulk-create · /role bulk-delete (AI-ONLY: hidden from Discord, see the note on each command) ──────────────────────────────
    # Same shape as /setup bulk-create: one command takes the WHOLE list, it is capped (modules/ai_bulk.py), the
    # confirm text is the exact plan, and the work runs one Discord call at a time (discord_bot/cogs/_ai_bulk_ops.py).
    # Roles are made with NO permissions (cosmetic: name, colour, hoist, mentionable) -- granting permissions stays a
    # deliberate human action. Deleting only touches roles below both the bot's and the asker's top role.
    AI_CONFIRMED_HANDLERS = {"role_create": "ai_rolecreate", "role_bulk_create": "ai_rolecreate",
                             "role_bulk_delete": "ai_roledelete"}
    AI_CONFIRM_PROMPTS = {"role_create": "ai_rolecreate_prompt", "role_bulk_create": "ai_rolecreate_prompt",
                          "role_bulk_delete": "ai_roledelete_prompt"}
    _COLOR_HELP = "Optional colour: a name (red, blue, purple...) or hex like #ff8800"
    AI_TOOL_PARAMS = {
        "create": [
            {"name": "name", "type": "string", "required": True, "description": "Name of the role to create"},
            {"name": "color", "type": "string", "description": _COLOR_HELP},
            {"name": "hoist", "type": "boolean", "description": "Show the role's members separately in the member list"},
            {"name": "mentionable", "type": "boolean", "description": "Let anyone @mention the role"},
        ],
        "bulk-create": [
            {"name": "names", "type": "string", "required": True,
             "description": f"Role names to create, comma-separated (max {_ai_bulk.MAX_CREATE_ROLES}), e.g. 'Member, VIP, Staff'. "
                            "ONE call with all the names \u2014 never one call per role."},
            {"name": "color", "type": "string", "description": _COLOR_HELP + " (applied to every role)"},
            {"name": "hoist", "type": "boolean", "description": "Show these roles' members separately in the member list"},
            {"name": "mentionable", "type": "boolean", "description": "Let anyone @mention these roles"},
        ],
        "bulk-delete": [
            {"name": "names", "type": "string", "required": True,
             "description": f"Roles to delete, comma-separated (max {_ai_bulk.MAX_DELETE_ROLES}), exact names. "
                            "ONE call with all the names \u2014 never one call per role."},
        ],
    }

    @staticmethod
    def _truthy(v) -> bool:
        return v is True or str(v).strip().lower() in ("1", "true", "yes", "y", "on")

    async def _role_create_plan(self, interaction, names, color=None, hoist=None, mentionable=None):
        """(error_or_None, plan). Every check that could make the job fail happens here, BEFORE the confirm button."""
        guild = interaction.guild
        if guild is None:
            return "This only works inside a server.", None
        if not guild.me.guild_permissions.manage_roles:
            return "I don't have **Manage Roles** in this server.", None
        if not interaction.user.guild_permissions.manage_roles:
            return "You need **Manage Roles** to create roles.", None
        colour = _ai_bulk.parse_color(color)
        if colour == -1:
            return f"I don't know the colour \u201c{color}\u201d \u2014 use a name like red/blue or hex like #ff8800.", None
        room = _ai_bulk.GUILD_ROLE_LIMIT - len(guild.roles)
        plan = _ai_bulk.plan_role_creation(names, [r.name for r in guild.roles], room)
        if not plan["create"]:
            why = []
            if plan["duplicates"]:
                why.append("already exist: " + _ai_bulk.summarize(plan["duplicates"]))
            if plan["invalid"]:
                why.append("not usable as names: " + _ai_bulk.summarize(plan["invalid"]))
            if room <= 0:
                why.append(f"the server is at Discord's {_ai_bulk.GUILD_ROLE_LIMIT}-role limit")
            return "Nothing to create" + (" \u2014 " + "; ".join(why) if why else ". Give me some names.") + ".", None
        plan.update(colour=colour, hoist=self._truthy(hoist), mentionable=self._truthy(mentionable))
        return None, plan

    @staticmethod
    def _role_create_summary(plan) -> str:
        bits = []
        if plan["colour"] is not None:
            bits.append(f"colour #{plan['colour']:06x}")
        if plan["hoist"]:
            bits.append("shown separately")
        if plan["mentionable"]:
            bits.append("mentionable")
        extra = f" ({', '.join(bits)})" if bits else ""
        text = (f"Create **{len(plan['create'])}** role(s){extra}, with no permissions: "
                f"{_ai_bulk.summarize(plan['create'])}")
        notes = []
        if plan["duplicates"]:
            notes.append("already exist, skipped: " + _ai_bulk.summarize(plan["duplicates"]))
        if plan["over_cap"]:
            notes.append(f"over the {_ai_bulk.MAX_CREATE_ROLES}-role limit, skipped: " + _ai_bulk.summarize(plan["over_cap"]))
        if plan["invalid"]:
            notes.append("not usable as names: " + _ai_bulk.summarize(plan["invalid"]))
        return text + ("\n_(" + "; ".join(notes) + ")_" if notes else "")

    async def _finish_role_create(self, interaction: discord.Interaction, plan: dict):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        busy = _ai_bulk.JOBS.try_start(guild.id)
        if busy:
            await interaction.followup.send(f"\u23f3 {busy}", ephemeral=True)
            return
        try:
            made, stop = await _ai_bulk_ops.create_roles(
                guild, plan["create"], plan["colour"], plan["hoist"], plan["mentionable"],
                reason=f"Role create by {interaction.user} ({interaction.user.id})")
        finally:
            _ai_bulk.JOBS.finish(guild.id)
        if made:
            await modx.log_action(guild.id, "role_create", interaction.user.id,
                                  reason=f"{len(made)} role(s): {_ai_bulk.summarize([r.name for r in made], 8)}")
        msg = (f"\u2705 Created {len(made)}/{len(plan['create'])}: " + " ".join(r.mention for r in made)) if made \
            else "\u274c Nothing was created."
        if stop:
            msg += f"\n\u26a0\ufe0f Stopped early: {stop}"
        await interaction.followup.send(msg[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _names_arg(names, name):
        # /role create passes one `name`; the bulk commands pass `names`. A single role name may contain a comma,
        # so for `name` it is kept whole.
        return [name] if name not in (None, "") else names

    async def ai_rolecreate_prompt(self, interaction, names=None, name=None, color=None, hoist=None,
                                   mentionable=None, **_ignored):
        err, plan = await self._role_create_plan(interaction, self._names_arg(names, name), color, hoist, mentionable)
        return (False, f"\u274c {err}") if err else (True, self._role_create_summary(plan) + "\nGo ahead?")

    async def ai_rolecreate(self, interaction, names=None, name=None, color=None, hoist=None,
                            mentionable=None, **_ignored):
        # Re-plan at click time: the server may have changed since the prompt was shown.
        err, plan = await self._role_create_plan(interaction, self._names_arg(names, name), color, hoist, mentionable)
        if err:
            await interaction.followup.send(f"\u274c {err}", ephemeral=True)
            return
        await self._finish_role_create(interaction, plan)

    async def _send_role_confirm(self, interaction, plan):
        from discord_bot.cogs._views_moderation import ConfirmActionView
        view = ConfirmActionView(interaction.user.id, lambda ci: self._finish_role_create(ci, plan),
                                 confirm_label="Create", danger=False)
        await interaction.response.send_message(self._role_create_summary(plan), view=view, ephemeral=True)

    # AI-ONLY: not a slash command people can see in Discord -- hidden before sync (AICommandSpec.ai_only in
    # modules/ai_command_allowlist.py, detach_ai_only_commands in modules/ai_command_guard.py); only the AI runs it.
    # WHY (owner's decision): typed by hand this is clunky and rarely used, and every slash command costs a slot of
    # Discord's 100-command cap the project is trying to free up. It is worth having only when someone describes
    # the job in plain words ("add a gold VIP role"). Keep the @command decorator --
    # the AI builds its tool schema and permission checks from it -- and do NOT re-expose this to Discord without asking.
    @group.command(name="create", description="Create one role (no permissions) with an optional colour")
    @app_commands.describe(name="Role name", color="Colour name (red, blue...) or hex like #ff8800",
                           hoist="Show members separately in the member list", mentionable="Let anyone @mention it")
    @app_commands.default_permissions(manage_roles=True)
    async def create_cmd(self, interaction: discord.Interaction, name: str, color: str = "",
                         hoist: bool = False, mentionable: bool = False):
        err, plan = await self._role_create_plan(interaction, [name], color, hoist, mentionable)
        if err:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
            return
        await self._send_role_confirm(interaction, plan)

    # AI-ONLY: not a slash command people can see in Discord -- hidden before sync (AICommandSpec.ai_only in
    # modules/ai_command_allowlist.py, detach_ai_only_commands in modules/ai_command_guard.py); only the AI runs it.
    # WHY (owner's decision): typed by hand this is clunky and rarely used, and every slash command costs a slot of
    # Discord's 100-command cap the project is trying to free up. It is worth having only when someone describes
    # the job in plain words ("make a role for each team, in different colours"). Keep the @command decorator --
    # the AI builds its tool schema and permission checks from it -- and do NOT re-expose this to Discord without asking.
    @group.command(name="bulk-create", description="Create several roles at once (no permissions)")
    @app_commands.describe(names="Role names, comma-separated (max 15)", color="Colour for all of them (name or hex)",
                           hoist="Show members separately in the member list", mentionable="Let anyone @mention them")
    @app_commands.default_permissions(manage_roles=True)
    async def bulkcreate_cmd(self, interaction: discord.Interaction, names: str, color: str = "",
                             hoist: bool = False, mentionable: bool = False):
        err, plan = await self._role_create_plan(interaction, names, color, hoist, mentionable)
        if err:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
            return
        await self._send_role_confirm(interaction, plan)

    async def _role_delete_plan(self, interaction, names):
        guild = interaction.guild
        if guild is None:
            return "This only works inside a server.", None
        if not guild.me.guild_permissions.manage_roles:
            return "I don't have **Manage Roles** in this server.", None
        if not interaction.user.guild_permissions.manage_roles:
            return "You need **Manage Roles** to delete roles.", None
        wanted = _ai_bulk.split_names(names)
        if not wanted:
            return "Tell me which roles to delete.", None
        if len(wanted) > _ai_bulk.MAX_DELETE_ROLES:
            return (f"That's {len(wanted)} roles \u2014 I delete at most {_ai_bulk.MAX_DELETE_ROLES} at a time. "
                    "Split it into smaller requests."), None
        res = _ai_bulk_ops.resolve_roles(guild, wanted, interaction.user)
        if not res["found"]:
            extra = _ai_bulk_ops.role_problems(res)
            return "Nothing to delete" + (f" \u2014 {extra}" if extra else "."), None
        return None, res

    @staticmethod
    def _role_delete_summary(res) -> str:
        names = [r.name for r in res["found"]]
        text = f"\u26a0\ufe0f **Permanently delete {len(names)} role(s):** {_ai_bulk.summarize(names)}. This can't be undone."
        extra = _ai_bulk_ops.role_problems(res)
        return text + (f"\n_({extra})_" if extra else "")

    async def _finish_role_delete(self, interaction: discord.Interaction, res: dict):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        busy = _ai_bulk.JOBS.try_start(guild.id)
        if busy:
            await interaction.followup.send(f"\u23f3 {busy}", ephemeral=True)
            return
        try:
            gone, stop = await _ai_bulk_ops.delete_roles(
                res["found"], reason=f"Bulk role delete by {interaction.user} ({interaction.user.id})")
        finally:
            _ai_bulk.JOBS.finish(guild.id)
        if gone:
            await modx.log_action(guild.id, "role_delete", interaction.user.id,
                                  reason=f"{len(gone)} role(s): {_ai_bulk.summarize(gone, 8)}")
        msg = (f"\U0001f5d1\ufe0f Deleted {len(gone)}/{len(res['found'])}: " + ", ".join(gone)) if gone \
            else "\u274c Nothing was deleted."
        if stop:
            msg += f"\n\u26a0\ufe0f Stopped early: {stop}"
        await interaction.followup.send(msg[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def ai_roledelete_prompt(self, interaction, names=None, **_ignored):
        err, res = await self._role_delete_plan(interaction, names)
        return (False, f"\u274c {err}") if err else (True, self._role_delete_summary(res) + "\nConfirm?")

    async def ai_roledelete(self, interaction, names=None, **_ignored):
        err, res = await self._role_delete_plan(interaction, names)     # re-resolved at click time
        if err:
            await interaction.followup.send(f"\u274c {err}", ephemeral=True)
            return
        await self._finish_role_delete(interaction, res)

    # AI-ONLY: not a slash command people can see in Discord -- hidden before sync (AICommandSpec.ai_only in
    # modules/ai_command_allowlist.py, detach_ai_only_commands in modules/ai_command_guard.py); only the AI runs it.
    # WHY (owner's decision): typed by hand this is clunky and rarely used, and every slash command costs a slot of
    # Discord's 100-command cap the project is trying to free up. It is worth having only when someone describes
    # the job in plain words ("remove the old event roles"). Keep the @command decorator --
    # the AI builds its tool schema and permission checks from it -- and do NOT re-expose this to Discord without asking.
    @group.command(name="bulk-delete", description="Delete several roles at once (asks to confirm)")
    @app_commands.describe(names="Role names, comma-separated (max 10)")
    @app_commands.default_permissions(manage_roles=True)
    async def bulkdelete_cmd(self, interaction: discord.Interaction, names: str):
        err, res = await self._role_delete_plan(interaction, names)
        if err:
            await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
            return
        from discord_bot.cogs._views_moderation import ConfirmActionView
        view = ConfirmActionView(interaction.user.id, lambda ci: self._finish_role_delete(ci, res),
                                 confirm_label="Delete", danger=True)
        await interaction.response.send_message(self._role_delete_summary(res), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleSetupCog(bot))
