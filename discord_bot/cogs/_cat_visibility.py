# path: discord_bot/cogs/_cat_visibility.py
"""Public-by-default cat screens that only their owner can use.

The cat game used to answer everything privately (ephemeral), which made it lonely. Menus and results now post in the
channel so others can see them, and every public screen is locked to the person who opened it: anybody else who taps
its buttons or menus gets a short private note and nothing happens (it never touches their data or the owner's).

What stays private on purpose: errors, cooldown notes, permission messages, server setup, and a hidden profile.
In DMs there is no channel to share in, so replies there are unchanged.
"""
from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
from typing import Optional

import discord

BACK_ID = "cat_nav_back"             # custom id of the Back button on a screen opened from the Cat menu
MAX_COMPONENTS = 40
menu_origin: ContextVar[Optional[dict]] = ContextVar("cat_menu_origin", default=None)
_hub_pictures: "OrderedDict[int, Optional[str]]" = OrderedDict()      # menu message id -> the cat picture on it


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
    origin = menu_origin.get()
    message = getattr(interaction, "message", None)
    if (origin is not None and message is not None and isinstance(view, discord.ui.LayoutView)
            and (show or getattr(interaction, "guild_id", None) is None)
            and not (set(kwargs) - {"view", "allowed_mentions"})):
        remember_hub(message.id, origin.get("picture_url"))        # a screen from the Cat menu replaces the menu card
        attach_back(view)
        mentions = kwargs["allowed_mentions"]
        if deferred:
            await interaction.edit_original_response(view=view, allowed_mentions=mentions)
        else:
            await interaction.response.edit_message(view=view, allowed_mentions=mentions)
        return
    if deferred:
        await interaction.followup.send(ephemeral=not show, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=not show, **kwargs)


# ── menu navigation: one message, in place, with a Back button ─────────────

def remember_hub(message_id: int, picture_url: Optional[str]) -> None:
    _hub_pictures[int(message_id)] = picture_url
    _hub_pictures.move_to_end(int(message_id))
    while len(_hub_pictures) > 500:
        _hub_pictures.popitem(last=False)


def hub_picture(message_id: Optional[int]) -> Optional[str]:
    return _hub_pictures.get(int(message_id)) if message_id is not None else None


def _walk(components):
    for c in components or []:
        yield c
        yield from _walk(getattr(c, "children", None))


def has_back(message) -> bool:
    """True when `message` is a Cat menu message currently showing a screen with a Back button."""
    return any(getattr(c, "custom_id", None) == BACK_ID for c in _walk(getattr(message, "components", None)))


def attach_back(view):
    """Adds a Back to menu button to the last card of a V2 screen. Stays inside Discord's 40-component limit: if the
    card is full it drops the footer line, then falls back to a free slot in the last button row, then does nothing."""
    if not isinstance(view, discord.ui.LayoutView):
        return view
    if any(getattr(i, "custom_id", None) == BACK_ID for i in view.walk_children()):
        return view
    box = next((c for c in reversed(view.children) if isinstance(c, discord.ui.Container)), None)
    if box is None:
        return view
    from discord_bot.cogs._views_cat_hub import BackToMenu
    used = sum(1 for _ in view.walk_children())
    footer = box.children[-1] if box.children else None
    footer = footer if isinstance(footer, discord.ui.TextDisplay) and str(footer.content).startswith("-#") else None
    if used + 2 <= MAX_COMPONENTS:
        if footer is not None:
            box.remove_item(footer)
        box.add_item(discord.ui.ActionRow(BackToMenu()))
        if footer is not None:
            box.add_item(footer)
        return view
    if footer is not None and used + 1 <= MAX_COMPONENTS:
        box.remove_item(footer)
        box.add_item(discord.ui.ActionRow(BackToMenu()))
        return view
    for row in reversed([c for c in box.children if isinstance(c, discord.ui.ActionRow)]):
        if len(row.children) < 5 and all(isinstance(b, discord.ui.Button) for b in row.children):
            row.add_item(BackToMenu())
            return view
    return view


async def edit(interaction: discord.Interaction, *, view: discord.ui.View, original: bool = False, **kwargs) -> None:
    """Edit the message the person tapped on. On a screen opened from the Cat menu the Back button is kept."""
    if has_back(getattr(interaction, "message", None)):
        attach_back(view)
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if original:                       # the reply was already deferred: edit the message it stands for
        await interaction.edit_original_response(view=view, **kwargs)
    else:
        await interaction.response.edit_message(view=view, **kwargs)
