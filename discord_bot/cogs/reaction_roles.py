"""
Button-based self-assignable roles — Discord equivalent of Carl-bot's
reaction roles. Uses discord.ui.Button components instead of actual emoji
reactions: buttons survive a bot restart via bot.add_view(view,
message_id=...) without needing the (privileged) message_content or
raw-reaction-tracking intents that classic emoji-reaction bots need, and
they're less error-prone for members (tap vs. finding the right emoji).

discord_reaction_roles (database.py) is the source of truth. On cog_load we
rebuild one persistent view per existing panel message so buttons posted
before a restart keep working immediately on reconnect — see
AnimeBotDiscord.setup_hook's docstring for why persistent views in general
must be registered before on_ready.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_shared import NavCardView, refresh_button

logger = logging.getLogger(__name__)

MAX_ROLES_PER_PANEL = 25  # Discord's per-view component limit


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    """Checks the invoking user's permission in the current channel.

    Uses interaction.permissions (always populated by Discord for any
    command run inside a guild channel) rather than
    interaction.user.guild_permissions, because interaction.user comes
    back as a plain discord.User instead of discord.Member when this app
    is invoked via a user-install context — even while run inside a real
    server channel — which made guild_permissions unreachable for anyone
    using the bot as a personal (user-installed) app, including owners.
    """
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


async def _deny(interaction: discord.Interaction, perm_name: str):
    msg = f"You need the **{perm_name}** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as premium.py/leveling.py/welcome.py: None on the
    main bot, the clone's row id on a clone process."""
    return getattr(interaction.client, "clone_id", None)


class ReactionRoleButton(discord.ui.Button):
    # Per (guild, user, role) lock, shared across all button instances —
    # serializes a double-click's read-then-write so the second click sees
    # the first's result instead of both reading "doesn't have it" off a
    # member-object cache that hasn't caught up yet.
    #
    # Reference-counted: a plain dict here would grow forever (one entry per
    # distinct guild/user/role combination ever toggled, for the life of the
    # process). _lock_refcounts tracks how many callers currently hold/await
    # each key so the entry can be dropped once nobody needs it anymore.
    _toggle_locks: dict = {}
    _lock_refcounts: dict = {}

    def __init__(self, role_id: int, label: str, emoji: str = None):
        super().__init__(
            label=label[:80],
            emoji=emoji or None,
            style=discord.ButtonStyle.secondary,
            custom_id=f"rr:{role_id}",
        )
        self.role_id = role_id

    @classmethod
    def _acquire_lock(cls, guild_id: int, user_id: int, role_id: int):
        key = (guild_id, user_id, role_id)
        lock = cls._toggle_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._toggle_locks[key] = lock
        cls._lock_refcounts[key] = cls._lock_refcounts.get(key, 0) + 1
        return key, lock

    @classmethod
    def _release_lock(cls, key):
        count = cls._lock_refcounts.get(key, 0) - 1
        if count <= 0:
            cls._lock_refcounts.pop(key, None)
            cls._toggle_locks.pop(key, None)
        else:
            cls._lock_refcounts[key] = count

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Couldn't resolve your server membership — try again.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("That role no longer exists — ask an admin to fix this panel.", ephemeral=True)
            return

        key, lock = self._acquire_lock(interaction.guild_id, member.id, self.role_id)
        try:
            async with lock:
                # Re-fetch under the lock rather than trusting member.roles —
                # the cached Member object isn't guaranteed to reflect a role
                # change made moments ago by the other branch of this same
                # race, even after that call has returned.
                try:
                    fresh_member = await interaction.guild.fetch_member(member.id)
                except discord.HTTPException:
                    fresh_member = member

                try:
                    if role in fresh_member.roles:
                        await fresh_member.remove_roles(role, reason="Reaction role button (toggle off)")
                        await interaction.response.send_message(f"➖ Removed **{role.name}**.", ephemeral=True)
                    else:
                        await fresh_member.add_roles(role, reason="Reaction role button (toggle on)")
                        await interaction.response.send_message(f"➕ Added **{role.name}**.", ephemeral=True)
                except discord.Forbidden:
                    await interaction.response.send_message(
                        "I don't have permission to manage that role — check my role hierarchy.", ephemeral=True
                    )
                except discord.HTTPException:
                    # e.g. a rate limit (429) — previously unhandled and would
                    # propagate out of the callback.
                    await interaction.response.send_message(
                        "Something went wrong updating your roles — please try again.", ephemeral=True
                    )
        finally:
            self._release_lock(key)


class ReactionRoleView(discord.ui.View):
    """timeout=None makes this a persistent view — required so it keeps
    working indefinitely (Discord buttons otherwise expire after 15
    minutes of view-object lifetime, though the message keeps showing them;
    persistence is what makes them keep WORKING)."""

    def __init__(self, roles: list):
        super().__init__(timeout=None)
        # De-dupe by role_id defensively: the DB's ON CONFLICT upsert
        # prevents this through the normal /reactionrole add path, but a
        # duplicate role_id in `roles` (e.g. an un-refreshed in-memory list
        # or a direct DB row) would otherwise produce two buttons sharing
        # one custom_id — an invalid Discord payload.
        seen_role_ids = set()
        deduped = []
        for r in roles:
            if r["role_id"] in seen_role_ids:
                continue
            seen_role_ids.add(r["role_id"])
            deduped.append(r)
        for r in deduped[:MAX_ROLES_PER_PANEL]:
            self.add_item(ReactionRoleButton(r["role_id"], r["label"], r.get("emoji")))


class ReactionRolesCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _find_panel_message(guild: discord.Guild, mid: int):
        """Looks for a panel message across every channel type that can host
        one. guild.text_channels alone misses threads (including forum
        posts) and any archived thread not currently cached — a panel
        posted in either of those was previously unfindable by /add or
        /remove even though it displays and works fine, since cog_load's
        persistent-view rebuild doesn't need a channel lookup at all."""
        candidates = list(guild.text_channels) + list(guild.threads)
        for channel in candidates:
            try:
                return await channel.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden):
                continue
        # Fall back to checking archived threads under each text/forum channel,
        # since guild.threads only surfaces threads currently cached as active.
        for channel in guild.text_channels:
            try:
                async for thread in channel.archived_threads(limit=100):
                    try:
                        return await thread.fetch_message(mid)
                    except (discord.NotFound, discord.Forbidden):
                        continue
            except (discord.Forbidden, AttributeError):
                continue
        return None

    async def cog_load(self):
        # Rebuild a persistent view for every panel THIS process owns.
        # Correction: an earlier version of this comment assumed a clone
        # only shares guilds the main bot doesn't run in — that's false (a
        # clone and the main bot can absolutely both be in the same guild,
        # per the expansion spec's core multi-tenancy model), so panels are
        # now scoped by clone_id too. Filtering here is what stops this
        # process from re-registering (and thus serving) another process's
        # buttons in a shared guild.
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            panels = await db.get_all_reaction_role_panels(clone_id=clone_id)
        except Exception as e:
            logger.error(f"[v0] Could not load reaction role panels on startup: {e}")
            return
        for panel in panels:
            if not panel["roles"]:
                continue
            view = ReactionRoleView(panel["roles"])
            self.bot.add_view(view, message_id=panel["message_id"])
        if panels:
            logger.info(f"[v0] Rebuilt {len(panels)} persistent reaction-role panel(s)")

    group = app_commands.guild_only()(app_commands.Group(name="reactionrole", description="Self-assignable role panels"))

    @group.command(name="create", description="Post a new (empty) reaction-role panel in this channel")
    @app_commands.describe(title="Panel title", description="Panel description")
    async def create(self, interaction: discord.Interaction, title: str, description: str = "Tap a button below to get a role."):
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        msg = await interaction.channel.send(embed=embed)
        confirm = discord.Embed(
            title="Panel created",
            description=f"Add roles to it with `/reactionrole add message_id:{msg.id} role:<role> label:<text>`.",
            color=discord.Color.blurple(),
        )
        confirm.set_footer(text=f"Message ID {msg.id}")
        await interaction.response.send_message(embed=confirm, ephemeral=True)

    @group.command(name="add", description="Add a role button to an existing panel")
    @app_commands.describe(message_id="The panel's message ID (from /reactionrole create)", role="Role to grant", label="Button text", emoji="Optional emoji shown on the button")
    async def add(self, interaction: discord.Interaction, message_id: str, role: discord.Role, label: str, emoji: str = None):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        if not message_id.isdigit():
            await interaction.followup.send("`message_id` must be numeric.", ephemeral=True)
            return
        mid = int(message_id)

        if role >= interaction.guild.me.top_role:
            await interaction.followup.send(
                "That role is above (or equal to) my own top role — I wouldn't be able to grant it. "
                "Move my role above it first.", ephemeral=True
            )
            return

        existing = await db.get_reaction_roles_for_message(mid)
        # Only the max-panel-size check for a genuinely NEW role — add_reaction_role
        # is an upsert (ON CONFLICT DO UPDATE), so re-adding a role already on a full
        # panel (e.g. to change its label/emoji) doesn't add a row and shouldn't be
        # blocked by the cap.
        already_present = any(r["role_id"] == role.id for r in existing)
        if not already_present and len(existing) >= MAX_ROLES_PER_PANEL:
            await interaction.followup.send(f"This panel already has the max of {MAX_ROLES_PER_PANEL} roles.", ephemeral=True)
            return

        msg = await self._find_panel_message(interaction.guild, mid)
        if msg is None:
            await interaction.followup.send("Couldn't find a panel message with that ID in this server.", ephemeral=True)
            return

        ok = await db.add_reaction_role(interaction.guild_id, msg.channel.id, mid, role.id, label, emoji, interaction.user.id, clone_id=_clone_id_of(interaction))
        if not ok:
            await interaction.followup.send("Something went wrong saving that role — try again.", ephemeral=True)
            return

        roles = await db.get_reaction_roles_for_message(mid)
        view = ReactionRoleView(roles)
        await msg.edit(view=view)
        self.bot.add_view(view, message_id=mid)  # re-register with the updated button set

        await interaction.followup.send(f"✅ Added **{role.name}** to the panel.", ephemeral=True)

    @group.command(name="remove", description="Remove a role button from a panel")
    @app_commands.describe(message_id="The panel's message ID", role="Role to remove from the panel")
    async def remove(self, interaction: discord.Interaction, message_id: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        if not message_id.isdigit():
            await interaction.followup.send("`message_id` must be numeric.", ephemeral=True)
            return
        mid = int(message_id)

        removed = await db.remove_reaction_role(mid, role.id)
        if not removed:
            await interaction.followup.send("That role wasn't on this panel.", ephemeral=True)
            return

        roles = await db.get_reaction_roles_for_message(mid)
        msg = await self._find_panel_message(interaction.guild, mid)
        if msg is not None:
            view = ReactionRoleView(roles) if roles else None
            await msg.edit(view=view)
            if view:
                self.bot.add_view(view, message_id=mid)

        await interaction.followup.send(f"✅ Removed **{role.name}** from the panel.", ephemeral=True)

    @group.command(name="list", description="List every reaction-role panel in this server")
    async def list_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panels = await db.get_reaction_role_panels_for_guild(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not panels:
            await interaction.followup.send("No reaction-role panels set up yet.", ephemeral=True)
            return
        lines = [f"**Panel {p['message_id']}**\n" + (", ".join(r["label"] for r in p["roles"]) or "(no roles yet)") for p in panels]
        buttons = [refresh_button(self, "list_panels")]
        card = NavCardView("Reaction-role panels", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))
