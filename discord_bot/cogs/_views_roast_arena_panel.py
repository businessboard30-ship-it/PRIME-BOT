# path: discord_bot/cogs/_views_roast_arena_panel.py

"""
Components V2 rendering for the inter-server roast arena's vs-card + live
vote/timer panel (see discord_bot/cogs/roast_arena.py). This module owns
LAYOUT only — it does not touch vote recording, timer math, or restart
persistence, all of which stay exactly as they were.

The two vote buttons are still the same DynamicArenaVoteChallengerButton /
DynamicArenaVoteChallengedButton from _views_roast_arena_challenge.py (same
custom_ids, same regex templates, same restart-proofing via
add_dynamic_items in bot.py) — this module just wraps them in an ActionRow
instead of a plain View, and wraps everything else in Container /
Section / TextDisplay / Separator instead of a discord.Embed.

RoastArenaPanelView is a discord.ui.LayoutView, so it is sent/edited via the
`view=` kwarg exactly like the old embed+View panel was — never `embed=`
alongside it. Discord rejects a message that mixes `content`/`embeds` with
Components V2 components, and discord.py enforces this: passing a
LayoutView as `view=` to send()/edit() sets the message's
IS_COMPONENTS_V2 flag for you (no manual discord.MessageFlags(...) needed),
and once a message carries that flag it can never be edited back to a
plain embed. That's why _edit_panel in roast_arena.py now passes
`embed=None, view=<LayoutView>` on every edit — there is no partial-update
path, each poller tick builds a brand-new RoastArenaPanelView from the
current vote counts and swaps the whole thing in.
"""

import discord

from discord_bot.cogs._views_roast_arena_challenge import (
    DynamicArenaVoteChallengedButton,
    DynamicArenaVoteChallengerButton,
)

# Discord's Components V2 palette is just a Colour int on the Container's
# left accent bar — same colors the old embed used.
_COLOR_LIVE = discord.Color.red()
_COLOR_ENDED = discord.Color.gold()


def _avatar_url(member: "discord.Member | None", guild: "discord.Guild | None") -> "str | None":
    """Best-effort avatar for a contestant's Section thumbnail. Falls back to
    the guild icon if the member isn't cached (e.g. they left, or the
    contestant slot is still unfilled pre-accept), and to None if neither
    is available — the caller drops the Section's accessory in that case
    rather than send a Thumbnail with no media."""
    if member is not None:
        return member.display_avatar.url
    if guild is not None and guild.icon is not None:
        return guild.icon.url
    return None


def _contestant_block(
    *, name: str, side_label: str, member: "discord.Member | None", guild: "discord.Guild | None",
    votes: int, pct: int,
) -> "discord.ui.Section | discord.ui.TextDisplay":
    """One contestant's slice of the vs-card: name + big vote number, with
    their avatar as a Section accessory when we have one to show."""
    text = f"### {side_label} {name}\n**{votes}** votes · {pct}%"
    avatar = _avatar_url(member, guild)
    if avatar:
        return discord.ui.Section(text, accessory=discord.ui.Thumbnail(media=avatar))
    return discord.ui.TextDisplay(text)


class RoastArenaPanelView(discord.ui.LayoutView):
    """The vs-card + live vote/timer panel, rebuilt on Components V2.

    Layout (all inside one accent-colored Container, replacing the old
    single discord.Embed):
      Section  — challenger name/avatar + live vote count
      Section  — challenged name/avatar + live vote count
      Separator
      TextDisplay — status line (countdown while live, winner once ended)
      ActionRow — the two vote buttons (omitted once ended, matching the old
                  build_locked_vote_view behavior — nothing to route to on a
                  finished panel)

    Built fresh every time the poller ticks or a vote lands; there's no
    in-place field mutation the way discord.Embed.set_field_at allowed, so
    keep construction here cheap.
    """

    def __init__(
        self,
        challenge: dict,
        counts: dict,
        *,
        challenger_name: str,
        challenged_name: str,
        challenger_member: "discord.Member | None" = None,
        challenged_member: "discord.Member | None" = None,
        challenger_guild: "discord.Guild | None" = None,
        challenged_guild: "discord.Guild | None" = None,
        ended: bool = False,
    ):
        super().__init__(timeout=None)

        total = counts["challenger"] + counts["challenged"]
        c_pct = round(100 * counts["challenger"] / total) if total else 0
        d_pct = round(100 * counts["challenged"] / total) if total else 0

        container = discord.ui.Container(accent_color=_COLOR_ENDED if ended else _COLOR_LIVE)

        header = "## 🏆 Roast battle — final result" if ended else "## ⚔️ Roast battle — LIVE"
        container.add_item(discord.ui.TextDisplay(header))

        container.add_item(_contestant_block(
            name=challenger_name, side_label="🔵", member=challenger_member,
            guild=challenger_guild, votes=counts["challenger"], pct=c_pct,
        ))
        container.add_item(_contestant_block(
            name=challenged_name, side_label="🔴", member=challenged_member,
            guild=challenged_guild, votes=counts["challenged"], pct=d_pct,
        ))

        container.add_item(discord.ui.Separator(visible=True))

        if ended:
            side = challenge.get("winner_side")
            if side == "challenger":
                status = f"**{challenger_name}** takes the crown. 🔵"
            elif side == "challenged":
                status = f"**{challenged_name}** takes the crown. 🔴"
            else:
                status = "It's a **draw** — the whole audience wins."
        else:
            ends_at = challenge.get("battle_ends_at")
            if ends_at:
                ts = int(ends_at.timestamp())
                status = f"Vote for the better roaster — voting closes <t:{ts}:R>."
            else:
                status = "Vote for the better roaster."
        status += "\n-# One vote per person · you can change it until the clock hits 0:00"
        container.add_item(discord.ui.TextDisplay(status))

        if not ended:
            challenge_id = challenge["id"]
            container.add_item(discord.ui.ActionRow(
                DynamicArenaVoteChallengerButton(challenge_id, challenger_name),
                DynamicArenaVoteChallengedButton(challenge_id, challenged_name),
            ))

        self.add_item(container)


def build_battle_panel(
    challenge: dict,
    counts: dict,
    *,
    challenger_name: str,
    challenged_name: str,
    challenger_member: "discord.Member | None" = None,
    challenged_member: "discord.Member | None" = None,
    challenger_guild: "discord.Guild | None" = None,
    challenged_guild: "discord.Guild | None" = None,
    ended: bool = False,
) -> RoastArenaPanelView:
    return RoastArenaPanelView(
        challenge,
        counts,
        challenger_name=challenger_name,
        challenged_name=challenged_name,
        challenger_member=challenger_member,
        challenged_member=challenged_member,
        challenger_guild=challenger_guild,
        challenged_guild=challenged_guild,
        ended=ended,
    )
