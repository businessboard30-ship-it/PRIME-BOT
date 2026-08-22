"""
Heist Wars — shared UI kit ("Nova City Operations Network").

Every embed in the game is built from the helpers in this file so the whole
feature reads as one product: same palette, same separators, same
progress-bar style, same status glyphs. Nothing here touches the database
or game logic — it only formats data it's handed.

Design system (see docs UI brief):
  cyan    -> technology / active state / neutral info
  violet  -> special / rare / cosmetic
  green   -> success
  amber   -> warning / risk / medium
  red     -> danger / failure / high risk
  gray    -> inactive / muted

Accessibility: every color-coded status is paired with a text glyph
(✓ / ⚠ / ✕ / ●) so nothing is communicated by color alone.
"""

from __future__ import annotations

import discord

from game.items import ItemDefinition, Rarity, RARITY_LABEL

# -- palette ------------------------------------------------------------

CYAN = discord.Color.from_rgb(0, 224, 255)
VIOLET = discord.Color.from_rgb(138, 92, 246)
GREEN = discord.Color.from_rgb(46, 213, 115)
AMBER = discord.Color.from_rgb(255, 176, 32)
RED = discord.Color.from_rgb(255, 71, 87)
GRAY = discord.Color.from_rgb(120, 130, 140)
CHARCOAL = discord.Color.from_rgb(20, 22, 28)

RARITY_COLOR: dict[Rarity, discord.Color] = {
    Rarity.COMMON: GRAY,
    Rarity.UNCOMMON: GREEN,
    Rarity.RARE: CYAN,
    Rarity.EPIC: VIOLET,
    Rarity.LEGENDARY: AMBER,
    Rarity.MYTHIC: RED,
}

RARITY_GLYPH = "◈"

SEP = "─" * 24


def progress_bar(pct: int, length: int = 10) -> str:
    """Compact block-character bar, e.g. '███████░░░ 72%'. Kept short so it
    never wraps awkwardly on mobile Discord."""
    pct = max(0, min(100, pct))
    filled = round((pct / 100) * length)
    return "█" * filled + "░" * (length - filled) + f" {pct}%"


def risk_label(pct: int) -> tuple[str, discord.Color, str]:
    """(text, color, glyph) for a threat/security/risk percentage."""
    if pct >= 70:
        return "HIGH", RED, "⚠"
    if pct >= 40:
        return "MEDIUM", AMBER, "●"
    return "LOW", GREEN, "✓"


def status_glyph(kind: str) -> str:
    return {"success": "✓", "warning": "⚠", "failed": "✕", "active": "●"}.get(kind, "●")


def op_title(text: str) -> str:
    return f"◈ {text.upper()}"


def base_embed(title: str, *, color: discord.Color = CYAN, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=op_title(title), description=description, color=color)
    embed.set_author(name="NOVA CITY // OPS — HEIST CONTROL SYSTEM")
    return embed


# -- rarity / item formatting ---------------------------------------------

def rarity_line(rarity: Rarity) -> str:
    return f"{RARITY_GLYPH} {RARITY_LABEL[rarity]}"


def item_line(item: ItemDefinition, *, owned: bool = False, equipped: bool = False, quantity: int | None = None) -> str:
    bits = [f"**{item.name}**", f"_{RARITY_LABEL[item.rarity]}_"]
    if equipped:
        bits.append("● EQUIPPED")
    elif owned:
        bits.append("● OWNED")
    if quantity and quantity > 1:
        bits.append(f"x{quantity}")
    return " — ".join(bits)


def item_detail_embed(item: ItemDefinition, *, owned: bool, equipped: bool, quantity: int = 1,
                       locked_reason: str | None = None) -> discord.Embed:
    color = RARITY_COLOR[item.rarity]
    embed = base_embed(item.name, color=color, description=f"_{item.description}_" if item.description else None)
    embed.add_field(name="CLASSIFICATION", value=f"{rarity_line(item.rarity)} // {item.category.value.upper()}", inline=False)

    if item.is_cosmetic:
        embed.add_field(name="EFFECT", value="Cosmetic only. No gameplay effect.", inline=False)
    elif item.effects:
        lines = []
        for eff in item.effects:
            phase = eff.phase.value.title() if eff.phase else "Global"
            lines.append(f"+{eff.magnitude}% — {eff.type.value.replace('_', ' ').title()} ({phase})")
        embed.add_field(name="EFFECT", value="\n".join(lines), inline=False)

    if item.flavor:
        embed.add_field(name="\u200b", value=f"_{item.flavor}_", inline=False)

    if locked_reason:
        embed.add_field(name="STATUS", value=f"✕ LOCKED — {locked_reason}", inline=False)
    elif equipped:
        embed.add_field(name="STATUS", value="✓ EQUIPPED", inline=False)
    elif owned:
        qty_note = f" (x{quantity})" if item.stackable and quantity > 1 else ""
        embed.add_field(name="STATUS", value=f"● OWNED{qty_note}", inline=False)
    else:
        embed.add_field(name="STATUS", value="○ NOT OWNED", inline=False)

    embed.set_footer(text=f"Required Level {item.required_level}" if item.required_level > 1 else "No level requirement")
    return embed


# -- misc formatting --------------------------------------------------------

def money(n: int) -> str:
    return f"${n:,}"


def fmt_minutes(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h {minutes % 60}m"
