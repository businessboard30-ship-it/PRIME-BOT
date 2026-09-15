# path: discord_bot/cogs/_views_leveling_leaderboard.py

"""
Components-v2 replacement for the old Pillow-rendered leaderboard image
(see discord_bot/cogs/leveling.py's old _build_leaderboard_payload, which
did an aiohttp avatar fetch per member + a PIL render on every single
/leaderboard call and every autopost tick). This builds a live
LayoutView/Container instead — avatars, names, and role colors are read
straight from Discord's own cache, no network fetch, no image render.

Two tabs, switched with a dropdown (StringSelect can't be a Section
accessory in Components v2, so it lives in its own ActionRow — same
pattern as _views_leveling_wizard.py's rate/announce/card selects):

  - "local"  — this guild's discord_xp rows only (clone_id-scoped, same
    as the original /leaderboard).
  - "global" — total_xp summed per user across EVERY guild and clone this
    bot (and all its clones) run in. Deliberately account-wide, not
    clone-scoped — see database.py's get_global_xp_leaderboard docstring.

Every control (mode select, prev/next/top, My Rank) is a persistent
DynamicItem, same restart-proof pattern as every other view in this
codebase (see _views_leveling_boost.py's BoostXPButton), because the
daily leaderboard autopost message needs to keep working after a
redeploy, not just for the lifetime of one /leaderboard invocation.

"Your Current Stats" reflects whoever most recently interacted with the
message (My Rank / a page turn / the initial /leaderboard invoker) — a
shared message has no single fixed "viewer", so this is the same
last-actor-wins tradeoff other shared/persistent panels in this codebase
already make (e.g. the wizard messages). The very first render (right
after /leaderboard or an autopost) has no clicker yet — autopost has none
at all, so that panel starts blank ("no stats yet, tap My Rank") until
someone clicks.

Per-entry accessory: a boosted user gets a small disabled button reading
their exact multiplier (e.g. "⚡ 2.0x active") instead of an avatar
thumbnail — that's the "own allocated button with a description of the
exact multiplier" a boosted entry gets that a non-boosted one doesn't.
Non-boosted entries get a Thumbnail(avatar) when the member/user object is
resolvable, or a plain disabled rank-number button as a last-resort
accessory (a Section always needs exactly one).
"""

import math
import re

import discord

from database import db
from modules import leveling
from discord_bot.cogs._views_leveling_boost import BoostXPButton

PAGE_SIZE = 10
MODES = ("local", "global")


def _clone_part(clone_id) -> str:
    return "-" if clone_id is None else str(clone_id)


def _clone_from(part: str):
    return None if part == "-" else int(part)


def _medal_or_rank(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")


async def _resolve_display(bot, guild, mode: str, user_id: int):
    """Returns (display_name, avatar_url, role_name, role_color) — role
    info only ever populated in local mode (global has no single guild's
    roles to show). Falls back to an API fetch when the user isn't
    cache-resolvable (guild.get_member/bot.get_user only check the local
    cache, which doesn't hold every member of a large guild without member-
    intent chunking) — this is why entries were showing raw "User 12345"
    IDs instead of names. Only falls back to a bare "User {id}" if the
    fetch itself fails (they left every mutual server, or the account no
    longer exists)."""
    member = guild.get_member(user_id) if (mode == "local" and guild) else None
    if member is None and mode == "local" and guild is not None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    user = member or bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None
    if user is None:
        return f"User {user_id}", None, None, None
    name = member.display_name if member else user.display_name
    avatar_url = str(user.display_avatar.replace(size=64).url)
    role_name = role_color = None
    if member is not None and member.top_role.name != "@everyone":
        role_name = member.top_role.name
        if member.top_role.color.value:
            role_color = str(member.top_role.color)
    return name, avatar_url, role_name, role_color


async def _stats_lines(bot, guild, clone_id, mode: str, user_id: int):
    """Builds the 'Your Current Stats' body for whoever last interacted.
    Returns None if they have no XP at all yet in that mode."""
    if mode == "local":
        rank_row = await db.get_xp_rank(guild.id, user_id, clone_id=clone_id)
    else:
        rank_row = await db.get_global_xp_rank(user_id)
    if rank_row is None or rank_row.get("total_xp") is None:
        return None
    rank = rank_row["rank"]
    total_players = rank_row["total_players"] or 1
    pct = round((rank / total_players) * 100, 1)
    name, _avatar, _role_name, _role_color = await _resolve_display(bot, guild, mode, user_id)
    return (
        f"**Your Current Stats**\n"
        f"User: {name}\n"
        f"Rank: #{rank} (Top {pct}%)\n"
        f"XP: {rank_row['total_xp']}"
    )


async def build_leaderboard_view(bot, guild: discord.Guild, clone_id, mode: str = "local",
                                  page: int = 0, stats_for_user_id=None) -> "discord.ui.LayoutView | None":
    """Shared builder for /leaderboard, the daily autopost loop, and every
    button/select callback below — exactly one place assembles the
    Container so all three stay in sync. Returns None only when there's
    truly nothing to show yet (no XP anywhere in scope)."""
    if mode == "local":
        total = await db.get_xp_leaderboard_count(guild.id, clone_id=clone_id)
    else:
        total = await db.get_global_xp_leaderboard_count()
    if not total:
        return None

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    offset = page * PAGE_SIZE

    if mode == "local":
        rows = await db.get_xp_leaderboard(guild.id, limit=PAGE_SIZE, clone_id=clone_id, offset=offset)
    else:
        rows = await db.get_global_xp_leaderboard(limit=PAGE_SIZE, offset=offset)

    user_ids = [r["user_id"] for r in rows]
    if mode == "local":
        boosts = await db.get_active_xp_boosts_for_users(guild.id, user_ids, clone_id=clone_id)
    else:
        boosts = await db.get_active_global_boosts_for_users(user_ids)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    mode_label = "Local" if mode == "local" else "Global"
    container.add_item(discord.ui.TextDisplay(
        f"### 📊 {mode_label} Level Leaderboard\nLeaderboard for user levels."
    ))
    mode_row = discord.ui.ActionRow()
    mode_row.add_item(LeaderboardModeSelect(guild.id, clone_id, mode))
    container.add_item(mode_row)

    if mode == "local":
        if guild.icon:
            guild_section = discord.ui.Section(accessory=discord.ui.Thumbnail(guild.icon.url))
            guild_section.add_item(f"**Guild:** {guild.name}")
            container.add_item(guild_section)
        else:
            container.add_item(discord.ui.TextDisplay(f"**Guild:** {guild.name}"))
    else:
        container.add_item(discord.ui.TextDisplay("**Global** — across all servers this bot runs in"))

    container.add_item(discord.ui.Separator())

    stats_text = None
    if stats_for_user_id is not None:
        stats_text = await _stats_lines(bot, guild, clone_id, mode, stats_for_user_id)
    stats_section = discord.ui.Section(accessory=LeaderboardMyRankButton(guild.id, clone_id, mode, page))
    stats_section.add_item(stats_text or "**Your Current Stats**\nTap *My Rank* to see where you stand.")
    container.add_item(stats_section)
    container.add_item(discord.ui.Separator())

    rankings_section = discord.ui.Section(accessory=LeaderboardNavButton(guild.id, clone_id, mode, page, "top"))
    rankings_section.add_item("**Rankings**")
    container.add_item(rankings_section)

    link_row = discord.ui.ActionRow()
    MAX_BOOST_BADGES = 2   # real Section+button entries; each costs 3 components
    MAX_LEADER_LINKS = 3   # link_row costs 1 (row) + N (buttons)
    ENTRY_DIVIDER = "\n-# ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    badge_budget = MAX_BOOST_BADGES
    chunk_lines = []

    def _flush_chunk():
        if chunk_lines:
            container.add_item(discord.ui.TextDisplay(ENTRY_DIVIDER.join(chunk_lines)))
            chunk_lines.clear()

    for i, row in enumerate(rows, start=offset + 1):
        name, avatar_url, role_name, _role_color = await _resolve_display(bot, guild, mode, row["user_id"])
        total_xp = row["total_xp"]
        level = row["level"] if mode == "local" else leveling.compute_level(total_xp)
        multiplier = boosts.get(row["user_id"])
        line = f"{_medal_or_rank(i)} {name}\nLvl `{level}` — {total_xp} xp"

        if multiplier and badge_budget > 0:
            # Real Section + disabled button ("own allocated button with the
            # exact multiplier") — capped at MAX_BOOST_BADGES per page so a
            # page that happens to be mostly-boosted users can never push
            # this message over Discord's 40-component ceiling (see the
            # ValueError this replaced). Any boosted entries past the cap
            # still show their multiplier, just inline as text instead of a
            # standalone button — flush whatever plain-text chunk is
            # pending first so ordering on the page stays correct.
            _flush_chunk()
            badge_budget -= 1
            accessory = discord.ui.Button(
                label=f"⚡ {multiplier:g}x active", style=discord.ButtonStyle.success,
                disabled=True, custom_id=f"lvllb_badge:{row['user_id']}:{i}",
            )
            entry_section = discord.ui.Section(accessory=accessory)
            entry_section.add_item(line)
            container.add_item(entry_section)
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        else:
            if multiplier:
                line += f" · ⚡ {multiplier:g}x active"
            chunk_lines.append(line)

        if mode == "local" and len(link_row.children) < MAX_LEADER_LINKS:
            link = await db.get_leader_link(guild.id, row["user_id"], clone_id=clone_id)
            if link and link["status"] == "approved":
                link_row.add_item(discord.ui.Button(
                    label=f"#{i} · {name}'s server", style=discord.ButtonStyle.link, url=link["invite_url"]
                ))

    _flush_chunk()

    container.add_item(discord.ui.TextDisplay(f"-# Total players: {total}"))
    container.add_item(discord.ui.Separator())

    nav_row = discord.ui.ActionRow()
    nav_row.add_item(LeaderboardNavButton(guild.id, clone_id, mode, page, "prev", disabled=(page <= 0)))
    nav_row.add_item(discord.ui.Button(label=f"{page + 1}/{total_pages}", style=discord.ButtonStyle.secondary,
                                        disabled=True, custom_id=f"lvllb_pageind:{guild.id}:{page}"))
    nav_row.add_item(LeaderboardNavButton(guild.id, clone_id, mode, page, "next", disabled=(page >= total_pages - 1)))
    container.add_item(nav_row)

    if link_row.children:
        container.add_item(link_row)

    container.add_item(discord.ui.Separator())
    boost_row = discord.ui.ActionRow()
    boost_row.add_item(BoostXPButton(guild.id, clone_id))
    container.add_item(boost_row)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, mode: str, page: int):
    # Ack the interaction FIRST, before any slow work — build_leaderboard_view
    # does DB queries and can fall back to a member/user API fetch per
    # unresolved entry (_resolve_display), which can easily blow past
    # Discord's 3-second component-interaction ack window. Deferring
    # up front means we always have the full 15-minute followup window to
    # actually edit the message, instead of racing the build against the
    # ack deadline and getting "404 Unknown interaction" when it loses.
    if not interaction.response.is_done():
        await interaction.response.defer()

    guild = interaction.client.get_guild(guild_id) or interaction.guild
    view = await build_leaderboard_view(
        interaction.client, guild, clone_id, mode=mode, page=page, stats_for_user_id=interaction.user.id,
    )
    if view is None:
        await interaction.edit_original_response(content="No XP earned yet.", view=None)
        return
    await interaction.edit_original_response(view=view)


class LeaderboardModeSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^lvllb_mode:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id, current_mode: str):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Select(
            placeholder=f"{'Local' if current_mode == 'local' else 'Global'} Level Leaderboard",
            min_values=1, max_values=1,
            custom_id=f"lvllb_mode:{guild_id}:{_clone_part(clone_id)}",
            options=[
                discord.SelectOption(label="Local Level Leaderboard", value="local",
                                      description="Just this server", default=(current_mode == "local")),
                discord.SelectOption(label="Global Level Leaderboard", value="global",
                                      description="Across every server", default=(current_mode == "global")),
            ],
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_id = _clone_from(match.group(2))
        return cls(guild_id, clone_id, "local")

    async def callback(self, interaction: discord.Interaction):
        new_mode = self.item.values[0]
        # Mode switch always resets to page 0 — a saved page number from
        # the other tab has no meaning once the underlying list changes.
        await _rerender(interaction, self.guild_id, self.clone_id, new_mode, 0)


class LeaderboardNavButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"^lvllb_nav:(\d+):(-|\d+):(local|global):(\d+):(prev|next|top)$"):
    def __init__(self, guild_id: int, clone_id, mode: str, page: int, action: str, disabled: bool = False):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.mode = mode
        self.page = page
        self.action = action
        labels = {"prev": "◀", "next": "▶", "top": "Top"}
        styles = {"prev": discord.ButtonStyle.secondary, "next": discord.ButtonStyle.secondary,
                  "top": discord.ButtonStyle.primary}
        super().__init__(discord.ui.Button(
            label=labels[action], style=styles[action], disabled=disabled,
            custom_id=f"lvllb_nav:{guild_id}:{_clone_part(clone_id)}:{mode}:{page}:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_id = _clone_from(match.group(2))
        mode = match.group(3)
        page = int(match.group(4))
        action = match.group(5)
        return cls(guild_id, clone_id, mode, page, action)

    async def callback(self, interaction: discord.Interaction):
        if self.action == "top":
            new_page = 0
        elif self.action == "prev":
            new_page = self.page - 1
        else:
            new_page = self.page + 1
        await _rerender(interaction, self.guild_id, self.clone_id, self.mode, new_page)


class LeaderboardMyRankButton(discord.ui.DynamicItem[discord.ui.Button],
                               template=r"^lvllb_myrank:(\d+):(-|\d+):(local|global):(\d+)$"):
    def __init__(self, guild_id: int, clone_id, mode: str, page: int):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.mode = mode
        self.page = page
        super().__init__(discord.ui.Button(
            label="My Rank", emoji="📊", style=discord.ButtonStyle.secondary,
            custom_id=f"lvllb_myrank:{guild_id}:{_clone_part(clone_id)}:{mode}:{page}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_id = _clone_from(match.group(2))
        mode = match.group(3)
        page = int(match.group(4))
        return cls(guild_id, clone_id, mode, page)

    async def callback(self, interaction: discord.Interaction):
        # Jump the page to wherever the clicker's own rank actually falls,
        # in whichever mode is currently active — not just re-render in
        # place, since "My Rank" implies "take me there".
        if self.mode == "local":
            rank_row = await db.get_xp_rank(self.guild_id, interaction.user.id, clone_id=self.clone_id)
        else:
            rank_row = await db.get_global_xp_rank(interaction.user.id)
        target_page = self.page
        if rank_row and rank_row.get("total_xp") is not None:
            target_page = (rank_row["rank"] - 1) // PAGE_SIZE
        await _rerender(interaction, self.guild_id, self.clone_id, self.mode, target_page)


DYNAMIC_ITEMS = (LeaderboardModeSelect, LeaderboardNavButton, LeaderboardMyRankButton)
