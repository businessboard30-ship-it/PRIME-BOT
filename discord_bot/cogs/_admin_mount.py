"""
Mount owner/admin commands that live in other cogs under the global /admin
group, so they share ONE top-level slash-command slot instead of one each
(Discord caps a bot at 100 global top-level commands).

Why this exists: discord.py registers a Group per cog and won't let a second
cog add children to it with the normal decorators (class-level groups/commands
with a `parent` are skipped by Cog.__new__, so they never get bound to the
cog). What DOES work is building an `app_commands.Command` from a BOUND method
in the cog's `cog_load` — discord.py takes the binding from `callback.__self__`,
so `self` is passed correctly and cog-level checks/error handlers still apply.

Usage, inside a cog (the method must NOT carry @app_commands.command):

    async def cog_load(self):
        mount_admin_command(self.bot, self.approvepayment,
                            name="approve", subgroup="payments",
                            description="Approve a pending payment")

    @app_commands.describe(reference="...")   # describe/choices/autocomplete
    async def approvepayment(self, interaction, reference: str): ...  # still work

AdminCog (discord_bot/cogs/admin.py) MUST be loaded before any cog that mounts
into it — bot.py loads it early for that reason. A missing AdminCog raises a
clear RuntimeError instead of silently dropping commands.

Limits to keep in mind: Discord allows /group subgroup subcommand (no deeper)
and at most 25 children per group. `/admin` has room for 25 direct children
(subgroups count as one each).
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from discord import app_commands

SUBGROUP_DESCRIPTIONS = {
    "payments": "[Admin] Manual payment review and routing",
    "broadcast": "[Owner] DM announcements and delivery status",
    "bump": "[Owner] Controls for the bump network",
}


def get_admin_group(bot) -> app_commands.Group:
    cog = bot.get_cog("AdminCog")
    if cog is None:
        raise RuntimeError(
            "AdminCog must be loaded before mounting commands under /admin "
            "(see the load order in discord_bot/bot.py)."
        )
    return cog.admin


def _subgroup(admin: app_commands.Group, name: str) -> app_commands.Group:
    existing = admin.get_command(name)
    if existing is not None:
        if not isinstance(existing, app_commands.Group):
            raise RuntimeError(f"/admin {name} exists and is not a subgroup")
        return existing
    # No `parent=` here: passing it registers the group on the parent itself,
    # and the explicit add_command below would then raise CommandAlreadyRegistered.
    group = app_commands.Group(
        name=name,
        description=SUBGROUP_DESCRIPTIONS.get(name, f"[Admin] {name}"),
    )
    admin.add_command(group)
    return group


def mount_admin_command(
    bot,
    callback: Callable,
    *,
    name: str,
    description: str,
    subgroup: Optional[str] = None,
    autocompletes: Optional[Mapping[str, Callable]] = None,
) -> app_commands.Command:
    """Attach a bound method as /admin [subgroup] <name>.

    `override=True` so a cog reload replaces its own stale command objects
    instead of failing with "already registered".
    """
    admin = get_admin_group(bot)
    target = _subgroup(admin, subgroup) if subgroup else admin
    cmd = app_commands.Command(name=name, description=description, callback=callback)
    for param, ac in (autocompletes or {}).items():
        cmd.autocomplete(param)(ac)
    target.add_command(cmd, override=True)
    return cmd


def mount_admin_group(bot, group: app_commands.Group, *, name: str) -> app_commands.Group:
    """Re-parent an already-bound cog Group (e.g. bump's old /bumpadmin) under
    /admin as /admin <name>. Call from cog_load, BEFORE the cog's commands are
    added to the tree (discord.py runs cog_load first), after removing the
    group from `cog.__cog_app_commands__` so it isn't also registered top-level.
    """
    admin = get_admin_group(bot)
    group.name = name
    admin.add_command(group, override=True)
    return group
