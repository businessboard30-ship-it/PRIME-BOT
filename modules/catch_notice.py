"""Refusals and errors as small styled embeds instead of bare text.

``notice_for(key, **fmt)`` returns ``{"embed": ...}`` ready to splat into ``send`` /
``send_message`` / ``edit_original_response``. The body is the unchanged locale text for ``key``;
the title and colour come from the kind (error, warning, info), looked up from ``KIND_FOR``.
Nothing here can raise: an unknown key is shown as info with the key's own text.
"""

from __future__ import annotations

import discord

from modules import catch_emoji as emoji
from modules.catch_i18n import text
from modules.catch_theme import state_color

# kind -> (theme state, emoji UI key)
KINDS = {"error": ("danger", "error"), "warning": ("warning", "warning"), "info": ("info", "info")}

_ERRORS = (
    "hub.error", "claim.error", "inventory.error", "daily.error", "profile.error", "collection.error",
    "dex.error", "status.error", "levelup.error", "wild.error", "creature.error", "evolve.error",
    "release.error", "sell.error", "sell.load_error", "shop.error", "shop.load_error", "wallet.error",
    "rules.error",
)
_WARNINGS = (
    "unavailable", "claim.blocked", "ui.not_yours", "collection.not_found",
    "creature.not_found", "dex.info.not_found", "claim.no_items", "release.gone", "sell.gone",
    "creature.nick_too_long", "creature.nick_blocked", "evolve.cannot", "release.refused_favorite",
    "release.refused_locked",
)
_INFOS = (
    "encounter.server_only", "encounter.cooldown", "shop.no_selection", "sell.no_selection",
    "creature.buddy_no_player", "creature.busy", "collection.busy",
)
KIND_FOR: dict[str, str] = {
    **{k: "error" for k in _ERRORS},
    **{k: "warning" for k in _WARNINGS},
    **{k: "info" for k in _INFOS},
}


def kind_of(key: str) -> str:
    bare = key[6:] if key.startswith("catch.") else key
    if bare in KIND_FOR:
        return KIND_FOR[bare]
    if bare.startswith(("evolve.refused_", "release.refused_", "creature.nick_")):
        return "warning"
    return "info"


def notice_embed(kind: str, key: str, **fmt: object) -> discord.Embed:
    state, icon = KINDS.get(kind, KINDS["info"])
    kind = kind if kind in KINDS else "info"
    title = f"{emoji.mark('ui', icon)} {text(f'notice.{kind}')}"
    return discord.Embed(title=title, description=text(key, **fmt)[:4096], colour=state_color(state))


def notice_for(key: str, **fmt: object) -> dict:
    """``{"embed": notice}`` for ``key``; kind chosen by ``kind_of``."""
    return {"embed": notice_embed(kind_of(key), key, **fmt)}


__all__ = ["KIND_FOR", "KINDS", "kind_of", "notice_embed", "notice_for"]
