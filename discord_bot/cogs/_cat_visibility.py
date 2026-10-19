# path: discord_bot/cogs/_cat_visibility.py
"""Public-by-default cat screens that only their owner can use.

The cat game used to answer everything privately (ephemeral), which made it lonely. Menus and results now post in the
channel so others can see them, and every public screen is locked to the person who opened it: anybody else who taps
its buttons or menus gets a short private note and nothing happens (it never touches their data or the owner's).

What stays private on purpose: errors, cooldown notes, permission messages, server setup, and a hidden profile.
In DMs there is no channel to share in, so replies there are unchanged.
"""
from __future__ import annotations

from typing import Optional

import discord


def is_public(interaction: discord.Interaction, public: bool = True) -> bool:
    """True when this reply should be visible to the whole channel."""
    return bool(public) and getattr(interaction, "guild_id", None) is not None


def message_is_v2(message) -> bool:
    """True when `message` is a Components V2 message. Public cat posts come in two kinds (older classic embed posts
    and newer V2 posts) and a message can't switch kind, so every edit of one checks this first."""
    return bool(getattr(getattr(message, "flags", None), "components_v2", False))


def lock_to_owner(view: Optional[discord.ui.View], owner_id: int):
    """Only `owner_id` can use `view`. Chains with the view's own interaction_check (some screens already have one).
    Safe to call twice. Returns the view for chaining."""
    if view is None or getattr(view, "_cat_owner_lock", None) == owner_id:
        return view
    previous = view.interaction_check

    async def check(interaction: discord.Interaction) -> bool:
        if interaction.user.id != owner_id:
            try:
                await interaction.response.send_message(
                    f"🐾 This one belongs to <@{owner_id}>. Type **/cat** to open your own!", ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            return False
        return await previous(interaction)

    view.interaction_check = check
    view._cat_owner_lock = owner_id
    return view


async def send(interaction: discord.Interaction, *, public: bool = True, view: Optional[discord.ui.View] = None,
               deferred: Optional[bool] = None, **kwargs) -> None:
    """Reply to `interaction` (fresh or already deferred; pass `deferred` when the caller knows, else it is looked up).
    Public replies lock their view to the opener."""
    show = is_public(interaction, public)
    if view is not None:
        kwargs["view"] = lock_to_owner(view, interaction.user.id) if show else view
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if deferred is None:
        is_done = getattr(interaction.response, "is_done", None)
        deferred = bool(is_done is not None and is_done())
    if deferred:
        await interaction.followup.send(ephemeral=not show, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=not show, **kwargs)
