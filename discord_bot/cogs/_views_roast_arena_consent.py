# path: discord_bot/cogs/_views_roast_arena_consent.py

"""
Persistent consent + event-invite buttons for the inter-server roast arena
(see discord_bot/cogs/roast_arena.py).

Two DM flows live here:

1. One-time consent DM — "enable inter-server roast battles?" with
   "enable it" / "not now". Sent once per guild (guarded by
   discord_roast_arena_config.consent_prompted), states the benefit and that
   nothing starts without further admin approval. Answering writes the
   guild's config and the DM is never re-sent.

2. Inter-server event invite DM — sent to OTHER rival server admins when a
   battle is about to start: "let members join" / "send as audience" /
   "apply to host next", plus a "remind me later" / "don't ask again" row.
   remind_after / dont_ask_again on the config row actually suppress future
   invite sends per-guild (roast_arena.py checks them before sending).

Every button is a discord.ui.DynamicItem[discord.ui.Button] with a regex
`template=` and a matching `custom_id`, exactly like
_views_registry_invite_consent.py — so the buttons keep working after a bot
restart (bot.py registers DYNAMIC_ITEMS via add_dynamic_items). We never use
a plain View with closures for anything that must persist.

Button callbacks that need to DO something in a guild (e.g. hand back a link
to the live battleground) reach the running cog via
`interaction.client.get_cog("RoastArenaCog")` rather than closing over it, so
a restored-after-restart button still functions.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord

from database import db

logger = logging.getLogger(__name__)

# How long "remind me later" suppresses further event invites for a guild.
REMIND_LATER_HOURS = 24


def _clone_id_of(client) -> "int | None":
    return getattr(client, "clone_id", None)


# ─────────────────────────────────────────────────────────────────────────
# Embed / View builders (the cog composes + sends these; the DM iteration
# over admins lives in the cog, same division of labour as roast.py).
# ─────────────────────────────────────────────────────────────────────────
def build_consent_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="⚔️ Enable inter-server roast battles?",
        description=(
            "Members challenge other servers, vote live, and laugh together. "
            "It's a fast way to bring in new members and give your community "
            "something to rally around. **No roast starts without your say.**"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="How it stays safe",
        value=(
            "• Every challenge is approved by an admin first\n"
            "• The bot only counts votes and the clock — it never judges\n"
            "• Works out of the box using the support server as the battleground"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Server: {guild.name} • ID: {guild.id}")
    return embed


def build_consent_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicArenaEnableButton(guild_id))
    view.add_item(DynamicArenaNotNowButton(guild_id))
    return view


def build_event_invite_embed(challenger_guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔔 {challenger_guild_name} wants to roast your server",
        description=(
            "Let your members join in, send them as an audience, or apply to "
            "host the next one."
        ),
        color=discord.Color.gold(),
    )
    return embed


def build_event_invite_view(challenge_id: int, guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicArenaInviteJoinButton(challenge_id, guild_id))
    view.add_item(DynamicArenaInviteAudienceButton(challenge_id, guild_id))
    view.add_item(DynamicArenaInviteHostButton(challenge_id, guild_id))
    view.add_item(DynamicArenaInviteRemindButton(guild_id))
    view.add_item(DynamicArenaInviteDontAskButton(guild_id))
    return view


# ─────────────────────────────────────────────────────────────────────────
# Consent DM buttons
# ─────────────────────────────────────────────────────────────────────────
class DynamicArenaEnableButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenaconsent:enable:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="enable it", style=discord.ButtonStyle.success, emoji="⚔️",
                custom_id=f"arenaconsent:enable:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        await db.upsert_roast_arena_config(
            self.guild_id, clone_id, enabled=True, consent_prompted=True
        )
        await interaction.edit_original_response(
            content="✅ Inter-server roast battles are **enabled**. Any member can now run `/roast challenge` — you'll still approve every incoming challenge before anything posts.",
            embed=None, view=None,
        )


class DynamicArenaNotNowButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenaconsent:notnow:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="not now", style=discord.ButtonStyle.secondary,
                custom_id=f"arenaconsent:notnow:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        # Mark prompted (so it's never auto-re-sent) but leave enabled FALSE.
        # An admin can still turn it on later with /roast battleground or by
        # running a challenge command's enable path.
        await db.upsert_roast_arena_config(
            self.guild_id, clone_id, enabled=False, consent_prompted=True
        )
        await interaction.edit_original_response(
            content="No problem — roast battles stay off. You can enable them any time with `/roast enable` in your server.",
            embed=None, view=None,
        )


# ─────────────────────────────────────────────────────────────────────────
# Event invite DM buttons
# ─────────────────────────────────────────────────────────────────────────
async def _battleground_link(client, challenge_id: int) -> "str | None":
    challenge = await db.get_roast_arena_challenge(challenge_id)
    if not challenge or not challenge.get("battleground_channel_id"):
        return None
    guild_id = challenge.get("battleground_guild_id")
    channel_id = challenge["battleground_channel_id"]
    if guild_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}"
    return None


class DynamicArenaInviteJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenainvite:join:(?P<challenge_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, challenge_id: int, guild_id: int):
        self.challenge_id = challenge_id
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="let members join", style=discord.ButtonStyle.success,
                custom_id=f"arenainvite:join:{challenge_id}:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        link = await _battleground_link(interaction.client, self.challenge_id)
        msg = "🎉 Great! Point your members to the battleground to jump in and vote:\n" + link if link else "🎉 Great! The battleground link will be shared once the battle goes live."
        await interaction.response.send_message(msg, ephemeral=True)


class DynamicArenaInviteAudienceButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenainvite:audience:(?P<challenge_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, challenge_id: int, guild_id: int):
        self.challenge_id = challenge_id
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="send as audience", style=discord.ButtonStyle.secondary,
                custom_id=f"arenainvite:audience:{challenge_id}:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        link = await _battleground_link(interaction.client, self.challenge_id)
        msg = "👀 Perfect — send your members over as audience to vote on the roasts:\n" + link if link else "👀 Perfect — the battleground link will be shared once the battle goes live."
        await interaction.response.send_message(msg, ephemeral=True)


class DynamicArenaInviteHostButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenainvite:host:(?P<challenge_id>\d+):(?P<guild_id>\d+)",
):
    def __init__(self, challenge_id: int, guild_id: int):
        self.challenge_id = challenge_id
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="apply to host next", style=discord.ButtonStyle.primary,
                custom_id=f"arenainvite:host:{challenge_id}:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["challenge_id"]), int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        # Set this guild's own custom battleground to nothing here, but flag
        # willingness by enabling the arena for them if they're opted in — the
        # simplest concrete "apply to host" that doesn't need a new table:
        # make sure they're enabled so their members can run /roast challenge
        # and be picked as a host battleground next time.
        clone_id = _clone_id_of(interaction.client)
        await db.upsert_roast_arena_config(self.guild_id, clone_id, enabled=True, consent_prompted=True)
        await interaction.response.send_message(
            "🙌 Noted — your server is enabled for roast battles, so it's in the pool to host and be challenged next time.",
            ephemeral=True,
        )


class DynamicArenaInviteRemindButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenainvite:remind:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="remind me later", style=discord.ButtonStyle.secondary,
                custom_id=f"arenainvite:remind:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        remind_after = datetime.now(timezone.utc) + timedelta(hours=REMIND_LATER_HOURS)
        await db.upsert_roast_arena_config(self.guild_id, clone_id, remind_after=remind_after)
        await interaction.edit_original_response(
            content=f"👍 Got it — I won't send another roast invite for about {REMIND_LATER_HOURS} hours.",
            embed=None, view=None,
        )


class DynamicArenaInviteDontAskButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"arenainvite:dontask:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label="don't ask again", style=discord.ButtonStyle.danger,
                custom_id=f"arenainvite:dontask:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        await db.upsert_roast_arena_config(self.guild_id, clone_id, dont_ask_again=True)
        await interaction.edit_original_response(
            content="Okay — I won't send you roast event invites anymore.",
            embed=None, view=None,
        )


# Registered in bot.py via add_dynamic_items so every button above survives a
# restart. Order doesn't matter — discord.py routes by the regex template.
DYNAMIC_ITEMS = (
    DynamicArenaEnableButton,
    DynamicArenaNotNowButton,
    DynamicArenaInviteJoinButton,
    DynamicArenaInviteAudienceButton,
    DynamicArenaInviteHostButton,
    DynamicArenaInviteRemindButton,
    DynamicArenaInviteDontAskButton,
)
