"""
"Find by name" button for every wizard channel dropdown.

Discord's native channel dropdown searches the channel's stored name, so a
channel called 📣【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】 never matches the word "welcome". The bot can't
change that search, so each channel dropdown gets a companion button that
opens a small text box and matches the typed text against channel names with
fonts/brackets/emoji folded away (modules.text_styles.plain_name).

How it plugs in (nothing per-wizard to edit):
  * install() wraps discord.ui.LayoutView.add_item. When a Container (or a bare
    ActionRow) holding a ChannelSelect is added, a second ActionRow with the
    button is inserted right after it — only if the 40-component limit allows.
  * The button is a DynamicItem (custom_id "chfind:<select custom_id>"), so it
    survives restarts like the rest of the wizards.
  * Picking a channel re-runs the ORIGINAL select's own callback with the chosen
    channel as its value, so each wizard's save + re-render logic is reused.

Only dropdowns that are DynamicItems inside a LayoutView are covered; plain
discord.ui.View selects are left alone.
"""
from __future__ import annotations

import logging
import re

import discord

from modules.text_styles import plain_name

logger = logging.getLogger(__name__)

MAX_COMPONENTS = 40
_PREFIX = "chfind:"
MAX_CUSTOM_ID = 100
_MAX_LISTED = 8


# ── matching ────────────────────────────────────────────────────────────────
def match_channels(channels, query: str, types=None) -> list:
    """Channels whose name matches `query`, ignoring fonts/brackets/emoji.
    Exact plain-name matches come first, then prefix matches, then the rest."""
    needle = plain_name(query)
    raw = (query or "").strip().lower()
    if not needle and not raw:
        return []
    hits = []
    for ch in channels:
        if types and getattr(ch, "type", None) not in types:
            continue
        plain = plain_name(ch.name)
        if (needle and needle in plain) or (raw and raw in ch.name.lower()):
            if needle and plain == needle:
                rank = 0
            elif needle and plain.startswith(needle):
                rank = 1
            else:
                rank = 2
            hits.append((rank, ch.position if hasattr(ch, "position") else 0, ch))
    hits.sort(key=lambda t: (t[0], t[1]))
    return [h[2] for h in hits]


def _select_types(message, target_custom_id: str):
    """channel_types of the select this button belongs to, read from the
    message's components, so the finder only offers channels the dropdown would."""
    try:
        stack = list(getattr(message, "components", []) or [])
        while stack:
            comp = stack.pop()
            if getattr(comp, "custom_id", None) == target_custom_id:
                types = getattr(comp, "channel_types", None)
                return list(types) if types else None
            stack.extend(getattr(comp, "children", []) or [])
    except Exception:
        logger.exception("[channel_finder] couldn't read channel_types")
    return None


# ── applying a pick through the original select ─────────────────────────────
async def _apply_pick(interaction: discord.Interaction, target_custom_id: str, channel) -> bool:
    store = interaction.client._connection._view_store
    for pattern, cls in list(store._dynamic_items.items()):
        match = pattern.fullmatch(target_custom_id)
        if match is None:
            continue
        placeholder = discord.ui.ChannelSelect(custom_id=target_custom_id)
        item = await cls.from_custom_id(interaction, placeholder, match)
        select = getattr(item, "item", None)
        if select is None:
            return False
        select._values = [channel]
        await item.callback(interaction)
        return True
    return False


class ChannelFindModal(discord.ui.Modal, title="Find a channel by name"):
    query = discord.ui.TextInput(
        label="Channel name (any part of it)",
        placeholder="e.g. welcome — works even if the name uses a fancy font or brackets",
        max_length=100,
    )

    def __init__(self, target_custom_id: str, types):
        super().__init__(timeout=300)
        self.target_custom_id = target_custom_id
        self.types = types

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return
        hits = match_channels(guild.channels, str(self.query), self.types)
        # Several matches, one of them an exact plain-name match → take it.
        if len(hits) > 1:
            needle = plain_name(str(self.query))
            exact = [c for c in hits if plain_name(c.name) == needle]
            if len(exact) == 1:
                hits = exact
        if not hits and self.types:
            other = match_channels(guild.channels, str(self.query))
            if other:
                kinds = {discord.ChannelType.news: "announcement channel", discord.ChannelType.voice: "voice channel",
                         discord.ChannelType.category: "category", discord.ChannelType.forum: "forum",
                         discord.ChannelType.stage_voice: "stage channel", discord.ChannelType.text: "text channel"}
                listed = "\n".join(f"• {c.mention} ({kinds.get(c.type, 'other type')})" for c in other[:_MAX_LISTED])
                await interaction.response.send_message(
                    "I found a match, but it's a type this dropdown doesn't accept:\n" + listed, ephemeral=True)
                return
        if not hits:
            await interaction.response.send_message(
                f"No channel matching **{discord.utils.escape_markdown(str(self.query))}** that this "
                f"dropdown can use. Try a shorter part of the name.", ephemeral=True)
            return
        if len(hits) > 1:
            listed = "\n".join(f"• {c.mention}" for c in hits[:_MAX_LISTED])
            more = f"\n…and {len(hits) - _MAX_LISTED} more" if len(hits) > _MAX_LISTED else ""
            await interaction.response.send_message(
                f"**{len(hits)} channels match** — type a bit more of the name:\n{listed}{more}",
                ephemeral=True)
            return
        try:
            ok = await _apply_pick(interaction, self.target_custom_id, hits[0])
        except Exception:
            logger.exception("[channel_finder] applying pick failed")
            ok = False
        if not ok and not interaction.response.is_done():
            await interaction.response.send_message(
                "I found the channel but couldn't apply it here — please use the dropdown instead.",
                ephemeral=True)


class ChannelFindButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^chfind:(?P<target>.+)$"):
    def __init__(self, target_custom_id: str):
        self.target_custom_id = target_custom_id
        super().__init__(discord.ui.Button(
            label="Find by name", emoji="🔎", style=discord.ButtonStyle.secondary,
            custom_id=f"{_PREFIX}{target_custom_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["target"])

    async def callback(self, interaction: discord.Interaction):
        types = _select_types(interaction.message, self.target_custom_id)
        await interaction.response.send_modal(ChannelFindModal(self.target_custom_id, types))


# ── auto-install on LayoutView ──────────────────────────────────────────────
def _channel_select_id(item):
    """custom_id if `item` is (or wraps) a ChannelSelect, else None."""
    inner = getattr(item, "item", item)
    if isinstance(inner, discord.ui.ChannelSelect):
        return inner.custom_id
    return None


def _row_select_id(row):
    for child in getattr(row, "children", []) or []:
        cid = _channel_select_id(child)
        if cid:
            return cid
    return None


def _find_row(target_custom_id: str):
    row = discord.ui.ActionRow()
    row.add_item(ChannelFindButton(target_custom_id))
    return row


def _augment_container(container, budget: int) -> int:
    """Insert a find-row after every channel-select row. Returns rows added."""
    added = 0
    children = container._children
    i = 0
    while i < len(children):
        row = children[i]
        cid = _row_select_id(row) if isinstance(row, discord.ui.ActionRow) else None
        already = (i + 1 < len(children) and isinstance(children[i + 1], discord.ui.ActionRow)
                   and any(isinstance(c, ChannelFindButton) for c in children[i + 1].children))
        if cid and not already and len(_PREFIX + cid) <= MAX_CUSTOM_ID and budget - (added * 2) >= 2:
            new_row = _find_row(cid)
            new_row._parent = container
            children.insert(i + 1, new_row)
            added += 1
            i += 1
        i += 1
    return added


_installed = False


def install() -> None:
    """Idempotent. Call once at startup."""
    global _installed
    if _installed:
        return
    original = discord.ui.LayoutView.add_item

    def add_item(self, item):
        try:
            budget = MAX_COMPONENTS - self.total_children_count - getattr(item, "_total_count", 1)
            if isinstance(item, discord.ui.Container) and budget >= 2:
                _augment_container(item, budget)
            elif isinstance(item, discord.ui.ActionRow) and budget >= 2:
                cid = _row_select_id(item)
                if cid and len(_PREFIX + cid) <= MAX_CUSTOM_ID:
                    original(self, item)
                    return original(self, _find_row(cid))
        except Exception:
            logger.exception("[channel_finder] couldn't add find button; sending view without it")
        return original(self, item)

    discord.ui.LayoutView.add_item = add_item
    _installed = True
    logger.info("[channel_finder] installed")


DYNAMIC_ITEMS = (ChannelFindButton,)
