# path: discord_bot/cogs/_views_ticket_panel.py

"""Components V2 screens of the ticket system: the panel members open tickets from, and the first message inside a ticket.
All rules (look, categories, names) come from modules/ticket_panel.py."""

from __future__ import annotations

from typing import Optional

import discord

from modules import ticket_panel as tp

OPEN_ID = "ticket:open"
SELECT_ID = "ticket:category"
CLAIM_ID = "ticket:claim"
CLOSE_ID = "ticket:close"


def server_icon_url(guild: discord.Guild) -> Optional[str]:
    icon = getattr(guild, "icon", None)
    return icon.url if icon else None


def _category_select(cats) -> discord.ui.Select:
    return discord.ui.Select(
        custom_id=SELECT_ID, placeholder=tp.PLACEHOLDER, min_values=1, max_values=1,
        options=[discord.SelectOption(label=c["label"], value=c["key"], description=c["description"] or None,
                                      emoji=c["emoji"]) for c in cats])


def open_button() -> discord.ui.Button:
    return discord.ui.Button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id=OPEN_ID)


def build_panel_view(guild: discord.Guild, cfg: dict, premium: bool) -> discord.ui.LayoutView:
    """The panel. Free: server icon + Open Ticket. Premium: the category dropdown, the custom look and link buttons."""
    import json
    look = tp.panel_look(cfg, premium)
    cats = tp.parse_categories(cfg.get("categories_json")) if premium else []
    lines = [f"-# {guild.name}", f"## {look['title']}", str(look["text"])]
    if cats:
        lines += ["", f"**{tp.categories_line(cats)}**", "Pick what fits best and the team gets the details first."]
    lines += ["", f"-# {guild.name} · Support"]
    head = discord.ui.TextDisplay("\n".join(lines)[:3900])
    box = discord.ui.Container(accent_colour=discord.Color(look["color"]))
    icon = server_icon_url(guild)
    if icon:
        box.add_item(discord.ui.Section(head, accessory=discord.ui.Thumbnail(icon, description=f"{guild.name} icon")))
    else:
        box.add_item(head)
    box.add_item(discord.ui.Separator())
    row = discord.ui.ActionRow()
    row.add_item(_category_select(cats) if cats else open_button())
    box.add_item(row)
    if premium:
        try:
            links = json.loads(cfg.get("custom_buttons_json") or "[]")
        except Exception:
            links = []
        link_row = discord.ui.ActionRow()
        for b in [b for b in links if isinstance(b, dict) and b.get("url")][:tp.MAX_CUSTOM_BUTTONS]:
            link_row.add_item(discord.ui.Button(label=str(b.get("label") or "Info")[:80], emoji=b.get("emoji") or None,
                                                url=b["url"], style=discord.ButtonStyle.link))
        if link_row.children:
            box.add_item(link_row)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


class TicketPanelLayout(discord.ui.LayoutView):
    """The persistent twin of the panel: one instance registered at startup answers every panel's dropdown and button,
    whatever options the panel was posted with (Discord routes by custom_id)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        select = discord.ui.Select(custom_id=SELECT_ID, placeholder=tp.PLACEHOLDER,
                                   options=[discord.SelectOption(label="General support", value="general")])
        select.callback = self._on_select
        button = open_button()
        button.callback = self._on_open
        box = discord.ui.Container()
        box.add_item(discord.ui.ActionRow(select))
        box.add_item(discord.ui.ActionRow(button))
        self.add_item(box)

    async def _on_select(self, interaction: discord.Interaction):
        values = (interaction.data or {}).get("values") or []
        await self.cog.handle_category(interaction, values[0] if values else None)

    async def _on_open(self, interaction: discord.Interaction):
        await self.cog.handle_open(interaction)


def ticket_view(opener: discord.abc.User, guild: discord.Guild, description: str, category: Optional[dict] = None,
                topic: Optional[str] = None, ping: str = "") -> discord.ui.LayoutView:
    """The first message in a ticket: who opened it, what about, and Claim / Close."""
    emoji = category["emoji"] if category else "🎫"
    label = category["label"] if category else "Support ticket"
    lines = [f"## {emoji} {label}", description]
    if topic:
        lines += ["", f"**{opener.display_name} wrote:**", "\n".join(f"> {ln}" for ln in topic.splitlines() if ln.strip())[:1500]]
    lines += ["", "-# Staff can Claim this ticket. The opener or staff can Close it and get a transcript."]
    box = discord.ui.Container(accent_colour=discord.Color(tp.DEFAULT_COLOR))
    if ping:
        box.add_item(discord.ui.TextDisplay(ping))
    head = discord.ui.TextDisplay("\n".join(lines)[:3900])
    icon = server_icon_url(guild)
    box.add_item(discord.ui.Section(head, accessory=discord.ui.Thumbnail(icon)) if icon else head)
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(
        discord.ui.Button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🙋", custom_id=CLAIM_ID),
        discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id=CLOSE_ID)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view
