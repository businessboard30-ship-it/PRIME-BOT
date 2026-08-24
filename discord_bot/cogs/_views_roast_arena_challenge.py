# path: discord_bot/cogs/_views_roast_arena_challenge.py

"""
Persistent challenge-lifecycle buttons for the inter-server roast arena
(see discord_bot/cogs/roast_arena.py):

  • approve / decline  — the DM to the CHALLENGED server's admin(s). Nothing
    is posted in their server until an admin approves.
  • accept             — the public post inside the challenged server; the
    first member to click becomes that server's contestant.
  • vote {side}        — the two vote buttons on the live battleground panel.
    One vote per user per challenge, changeable up until 0:00 (the cog upserts
    the vote, so re-clicking the other side just flips your existing vote).

Every button is a discord.ui.DynamicItem[discord.ui.Button] with a regex
`template=` + matching `custom_id`, identical to the pattern in
_views_registry_invite_consent.py, so all of them survive a bot restart
(bot.py registers DYNAMIC_ITEMS). Callbacks delegate the actual work back to
the running cog via `interaction.client.get_cog("RoastArenaCog")` so a
restored button still drives the real flow.
"""

import logging

import discord

logger = logging.getLogger(__name__)

_COG_NAME = "RoastArenaCog"


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog(_COG_NAME)


# ─────────────────────────────────────────────────────────────────────────
# Builders (the cog composes embeds + sends; these keep the button wiring in
# one place).
# ─────────────────────────────────────────────────────────────────────────
def build_approval_view(challenge_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicArenaApproveButton(challenge_id))
    view.add_item(DynamicArenaDeclineButton(challenge_id))
    return view


def build_accept_view(challenge_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicArenaAcceptButton(challenge_id))
    return view


def build_vote_view(
    challenge_id: int,
    challenger_name: "str | None" = None,
    challenged_name: "str | None" = None,
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicArenaVoteChallengerButton(challenge_id, challenger_name))
    view.add_item(DynamicArenaVoteChallengedButton(challenge_id, challenged_name))
    return view


def build_locked_vote_view(
    challenger_name: "str | None" = None, challenged_name: "str | None" = None
) -> discord.ui.View:
    """A non-interactive view of two disabled buttons for the finished panel —
    plain Buttons (no custom_id) because nothing needs to route to them once
    the battle is over."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=f"vote {challenger_name}" if challenger_name else "vote challenger",
        style=discord.ButtonStyle.primary, disabled=True,
    ))
    view.add_item(discord.ui.Button(
        label=f"vote {challenged_name}" if challenged_name else "vote challenged",
        style=discord.ButtonStyle.danger, disabled=True,
    ))
    return view


# ─────────────────────────────────────────────────────────────────────────
# Approve / decline (challenged server's admin DM)
# ─────────────────────────────────────────────────────────────────────────
class DynamicArenaApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenachallenge:approve:(?P<challenge_id>\d+)",
):
    def __init__(self, challenge_id: int):
        self.challenge_id = challenge_id
        super().__init__(
            discord.ui.Button(
                label="approve", style=discord.ButtonStyle.success, emoji="✅",
                custom_id=f"arenachallenge:approve:{challenge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_admin_approve(interaction, self.challenge_id)


class DynamicArenaDeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenachallenge:decline:(?P<challenge_id>\d+)",
):
    def __init__(self, challenge_id: int):
        self.challenge_id = challenge_id
        super().__init__(
            discord.ui.Button(
                label="decline", style=discord.ButtonStyle.danger, emoji="✋",
                custom_id=f"arenachallenge:decline:{challenge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_admin_decline(interaction, self.challenge_id)


# ─────────────────────────────────────────────────────────────────────────
# Accept (public post in the challenged server)
# ─────────────────────────────────────────────────────────────────────────
class DynamicArenaAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenaaccept:(?P<challenge_id>\d+)",
):
    def __init__(self, challenge_id: int):
        self.challenge_id = challenge_id
        super().__init__(
            discord.ui.Button(
                label="accept the challenge 🔥", style=discord.ButtonStyle.danger,
                custom_id=f"arenaaccept:{challenge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.on_member_accept(interaction, self.challenge_id)


# ─────────────────────────────────────────────────────────────────────────
# Vote buttons (live battleground panel)
# ─────────────────────────────────────────────────────────────────────────
class DynamicArenaVoteChallengerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenavote:challenger:(?P<challenge_id>\d+)",
):
    def __init__(self, challenge_id: int, name: "str | None" = None):
        self.challenge_id = challenge_id
        # Label carries the contestant name when built fresh; after a restart
        # from_custom_id has no name, so it falls back to a generic label —
        # the custom_id (and therefore vote routing) is unaffected.
        super().__init__(
            discord.ui.Button(
                label=f"vote {name}" if name else "vote challenger",
                style=discord.ButtonStyle.primary,
                custom_id=f"arenavote:challenger:{challenge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.handle_vote(interaction, self.challenge_id, "challenger")


class DynamicArenaVoteChallengedButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenavote:challenged:(?P<challenge_id>\d+)",
):
    def __init__(self, challenge_id: int, name: "str | None" = None):
        self.challenge_id = challenge_id
        super().__init__(
            discord.ui.Button(
                label=f"vote {name}" if name else "vote challenged",
                style=discord.ButtonStyle.danger,
                custom_id=f"arenavote:challenged:{challenge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        await cog.handle_vote(interaction, self.challenge_id, "challenged")


# Registered in bot.py via add_dynamic_items so every button survives restart.
DYNAMIC_ITEMS = (
    DynamicArenaApproveButton,
    DynamicArenaDeclineButton,
    DynamicArenaAcceptButton,
    DynamicArenaVoteChallengerButton,
    DynamicArenaVoteChallengedButton,
)
