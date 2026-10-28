# path: discord_bot/cogs/_views_xp_wizard.py

"""
The leaderboard's "XP & Clans" wizard: ONE private message with four tabs that edits itself in place, instead of a loose
row of buttons that each pop open another message.

  ⚡ Boost XP     your own boost: the Level Pass and the multi-boost packs, bought right here
  🔔 Pings        level-up pings on or off, with the current state shown
  🏰 Clans        pick a clan and page through its members
  🌍 Server boost buy a server-wide XP boost for everyone
  🎴 Card Hub     a button under every tab opens the card hub (Store, collection, profile, rankings) in the same message

A summary of your boost, the server boost and your ping setting sits at the top of every tab. Every purchase goes through
the same checkout code as before (_views_leveling_boost._start_boost_checkout and BuyBoostSelect), so prices, products
and payment modes are unchanged. Nothing here writes anything except the ping toggle (db.toggle_level_ping_optout).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord

import config as app_config
from database import db

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
TABS = (("boost", "Boost XP", "⚡"), ("pings", "Pings", "🔔"), ("clans", "Clans", "🏰"), ("server", "Server boost", "🌍"))
CLAN_PAGE = 10


def _ts(value) -> Optional[int]:
    try:
        return int(value.timestamp())
    except Exception:
        logger.debug("swallowed exception in _ts", exc_info=True)
        return None


def _boost_line(row, who: str) -> str:
    if not row:
        return f"**{who}:** none right now"
    until = _ts(row.get("expires_at"))
    mult = row.get("multiplier") or app_config.XP_BOOST_MULTIPLIER
    return f"**{who}:** ⚡ {float(mult):g}x XP" + (f" until <t:{until}:R>" if until else "")


def _cp(clone_id) -> str:
    return "-" if clone_id is None else str(clone_id)


def _cf(part: str):
    return None if part == "-" else int(part)


class XPWizardState:
    """Everything the wizard shows, rebuilt from (guild, clone, user, tab, clan, page) on every click."""

    def __init__(self, guild_id: int, clone_id, user_id: int, tab: str = "boost", clan: Optional[str] = None,
                 page: int = 0):
        self.guild_id, self.clone_id, self.user_id = guild_id, clone_id, user_id
        self.tab = tab if tab in {t[0] for t in TABS} else "boost"
        self.boost = self.server_boost = None
        self.muted = False
        self.bundles: dict = {}
        self.offer_pass = False
        self.clan: Optional[str] = clan
        self.page = max(0, int(page))
        self.clan_total = 0
        self.clan_lines: list = []
        self.guild = None

    # ---- data ----
    async def load(self) -> None:
        """Everything the summary and the tabs show. A failing lookup shows as 'unknown', never breaks the wizard."""
        from discord_bot.cogs import _views_leveling_boost as boost_mod

        async def safe(coro, default):
            try:
                return await coro
            except Exception:
                logger.warning("[xp-wizard] a lookup failed", exc_info=True)
                return default
        self.boost, self.server_boost, muted, self.offer_pass = await asyncio.gather(
            safe(db.get_active_xp_boost(self.guild_id, self.user_id, clone_id=self.clone_id), None),
            safe(db.get_active_guild_xp_boost(self.guild_id, clone_id=self.clone_id), None),
            safe(db.get_level_ping_muted(self.guild_id, [self.user_id]), set()),
            safe(boost_mod.pass_offered(self.clone_id), False))
        self.muted = self.user_id in muted
        self.bundles = boost_mod._available_bundles()

    async def _load_clan_page(self) -> None:
        if not self.clan:
            self.clan_lines, self.clan_total = [], 0
            return
        members, total = await asyncio.gather(
            db.get_clan_members(self.guild_id, self.clan, clone_id=self.clone_id, limit=CLAN_PAGE, offset=self.page * CLAN_PAGE),
            db.get_clan_member_count(self.guild_id, self.clan, clone_id=self.clone_id))
        self.clan_total = int(total or 0)
        lines = []
        for i, m in enumerate(members, start=self.page * CLAN_PAGE + 1):
            obj = self.guild.get_member(m["user_id"]) if self.guild else None
            name = discord.utils.escape_markdown(obj.display_name) if obj else f"<@{m['user_id']}>"
            lines.append(f"`#{i}` {name} · Lvl `{m['level']}` ({m['total_xp']} xp)")
        self.clan_lines = lines

    async def refresh(self, interaction: discord.Interaction, *, reload: bool = True, clan_page: bool = True) -> None:
        """Re-read what the current tab shows. Never raises: a failed lookup just leaves older numbers."""
        self.guild = interaction.guild or self.guild
        try:
            if reload:
                await self.load()
            if self.tab == "clans" and clan_page:
                await self._load_clan_page()
        except Exception:
            logger.warning("[xp-wizard] redraw lookup failed", exc_info=True)

    # ---- drawing ----
    def text(self) -> str:
        summary = [_boost_line(self.boost, "Your XP boost"), _boost_line(self.server_boost, "Server boost"),
                   f"**Level-up pings:** {'🔕 off' if self.muted else '🔔 on'}"]
        name = {t[0]: f"{t[2]} {t[1]}" for t in TABS}[self.tab]
        if self.tab == "boost":
            title, body = name, self._boost_text()
        elif self.tab == "pings":
            title = name
            body = ("🔕 **Off.** Level-up announcements still appear, but I won't @ping you." if self.muted else
                    "🔔 **On.** I'll @ping you when you level up.")
        elif self.tab == "clans":
            if not self.clan:
                title, body = name, "Pick a clan below to see its members."
            else:
                from modules import clan_cards
                title = (f"{name} · {clan_cards.get_clan_label(self.clan)} · {self.clan_total} member"
                         f"{'' if self.clan_total == 1 else 's'}")
                body = ("\n".join(self.clan_lines) or "Nobody's been assigned to this clan here yet.")[:1000]
        else:
            tiers = [f"• **{t['label']}** · ${t['fee_usd']:g} · {t['multiplier']:g}x XP for everyone, {t['duration_hours']}h"
                     for k, t in app_config.XP_SERVER_BOOST_TIERS.items()]
            title = name
            body = "Everyone in the server levels faster for a while.\n" + "\n".join(tiers)
        return "\n".join(["## ⚡ XP & Clans", *summary, "", f"### {title}", body, "", "-# Only you can see this."])

    def _boost_text(self) -> str:
        if not self.bundles and not self.offer_pass:
            return "⚡ XP boosts aren't on sale right now. Try again in a little while."
        lines = ["Boosts stack onto any boost time you already have."]
        if self.offer_pass:
            lines.append(f"• **🎫 Level Pass** · ${app_config.LEVEL_PASS_FEE_USD:g} once · your own level-up card for "
                         f"{app_config.LEVEL_PASS_CARD_DAYS} days **plus** {app_config.XP_BOOST_MULTIPLIER:g}x XP for "
                         f"{app_config.XP_BOOST_DURATION_DAYS} days. One payment, no subscription.")
        for b in self.bundles.values():
            days = b["boosts"] * app_config.XP_BOOST_DURATION_DAYS
            lines.append(f"• **{b['label']}** ({b['boosts']} boosts) · ${b['fee_usd']:g} · "
                         f"{app_config.XP_BOOST_MULTIPLIER:g}x XP for {days} days")
        return "\n".join(lines)

    def build(self, controls: bool = True) -> discord.ui.LayoutView:
        """The whole message: text on top, then tabs on the first row and the active tab's own controls below."""
        view = discord.ui.LayoutView(timeout=None)
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.TextDisplay(self.text()))
        if controls:
            box.add_item(discord.ui.Separator())
            g, c = self.guild_id, self.clone_id
            box.add_item(discord.ui.ActionRow(
                *[XPTabButton(g, c, key, label, emoji, active=(key == self.tab)) for key, label, emoji in TABS],
                XPCloseButton()))
            if self.tab == "boost":
                self._add_buy_buttons(box)
            elif self.tab == "pings":
                box.add_item(discord.ui.ActionRow(XPPingToggle(g, c, self.muted)))
            elif self.tab == "clans":
                box.add_item(discord.ui.ActionRow(XPClanSelect(g, c, self.clan)))
                if self.clan:
                    pages = max(1, -(-self.clan_total // CLAN_PAGE))
                    box.add_item(discord.ui.ActionRow(
                        XPPageButton(g, c, self.clan, max(0, self.page - 1), "◀", disabled=self.page <= 0),
                        discord.ui.Button(label=f"{self.page + 1}/{pages}", disabled=True,
                                          style=discord.ButtonStyle.secondary, custom_id="xpw:pagenum"),
                        XPPageButton(g, c, self.clan, self.page + 1, "▶", disabled=self.page >= pages - 1)))
            else:
                from discord_bot.cogs._views_leveling_wallet import BuyBoostSelect
                box.add_item(discord.ui.ActionRow(BuyBoostSelect(g, c)))
            # The card hub lives one click away from every tab (no slash command, no extra leaderboard button: that page
            # is already at Discord's component ceiling). It opens in this same private message.
            from discord_bot.cogs._views_card_hub import HubButton
            box.add_item(discord.ui.ActionRow(HubButton("home", c, g, "", "Card Hub", "🎴")))
        view.add_item(box)
        return view

    def _add_buy_buttons(self, box: discord.ui.Container) -> None:
        from discord_bot.cogs._views_leveling_boost import BoostBuyButton
        items = []
        if self.offer_pass:
            items.append(BoostBuyButton(self.guild_id, "level_pass", f"Level Pass · ${app_config.LEVEL_PASS_FEE_USD:g}", emoji="🎫"))
        items += [BoostBuyButton(self.guild_id, k, f"{b['label']} · ${b['fee_usd']:g}") for k, b in self.bundles.items()]
        items = items[:10]
        for i in range(0, len(items), 5):
            box.add_item(discord.ui.ActionRow(*items[i:i + 5]))


def _state_from(interaction: discord.Interaction, g, c, tab="boost", clan=None, page=0) -> XPWizardState:
    st = XPWizardState(g, c, interaction.user.id, tab, clan, page)
    st.guild = interaction.guild
    return st


async def _redraw(interaction: discord.Interaction, st: XPWizardState, **kw) -> None:
    await st.refresh(interaction, **kw)
    await interaction.response.edit_message(view=st.build())


# ---- persistent controls: state lives in the custom_id, data is re-read on every click ----

class XPTabButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^xpw:tab:(\d+):(-|\d+):([a-z_]+)$"):
    def __init__(self, guild_id: int, clone_id, tab: str, label: str = "", emoji: str = None, active: bool = False):
        self.guild_id, self.clone_id, self.tab = guild_id, clone_id, tab
        super().__init__(discord.ui.Button(
            label=label or tab, emoji=emoji, custom_id=f"xpw:tab:{guild_id}:{_cp(clone_id)}:{tab}",
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _cf(match.group(2)), match.group(3))

    async def callback(self, interaction: discord.Interaction):
        # a boost may have just been bought: always show the fresh state
        await _redraw(interaction, _state_from(interaction, self.guild_id, self.clone_id, self.tab), reload=True)


class XPCloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^xpw:close$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id="xpw:close"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.LayoutView(timeout=None)
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.TextDisplay("## ⚡ XP & Clans\nClosed. Tap **XP & Clans** on the leaderboard to open it again."))
        view.add_item(box)
        await interaction.response.edit_message(view=view)


class XPPingToggle(discord.ui.DynamicItem[discord.ui.Button], template=r"^xpw:ping:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id, muted: bool = False):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label="Turn pings ON" if muted else "Turn pings OFF", emoji="🔔" if muted else "🔕",
            style=discord.ButtonStyle.success if muted else discord.ButtonStyle.danger,
            custom_id=f"xpw:ping:{guild_id}:{_cp(clone_id)}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _cf(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        muted = await db.toggle_level_ping_optout(self.guild_id, interaction.user.id)
        st = _state_from(interaction, self.guild_id, self.clone_id, "pings")
        await st.refresh(interaction, reload=True)
        st.muted = muted                                    # the value the toggle just wrote
        await interaction.response.edit_message(view=st.build())


class XPClanSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^xpw:clan:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id, selected: Optional[str] = None):
        from modules.clan_cards import CLAN_CARDS
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Select(
            placeholder="Choose a clan…", custom_id=f"xpw:clan:{guild_id}:{_cp(clone_id)}",
            options=[discord.SelectOption(label=label, value=fn, default=(fn == selected))
                     for fn, label, *_ in CLAN_CARDS]))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _cf(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        st = _state_from(interaction, self.guild_id, self.clone_id, "clans", self.item.values[0], 0)
        await _redraw(interaction, st, reload=True)


class XPPageButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"^xpw:pg:(\d+):(-|\d+):([A-Za-z0-9_.\-]+):(\d+)$"):
    def __init__(self, guild_id: int, clone_id, clan: str, page: int, label: str = "", disabled: bool = False):
        self.guild_id, self.clone_id, self.clan, self.page = guild_id, clone_id, clan, page
        super().__init__(discord.ui.Button(
            label=label or str(page), style=discord.ButtonStyle.secondary, disabled=disabled,
            custom_id=f"xpw:pg:{guild_id}:{_cp(clone_id)}:{clan}:{page}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _cf(match.group(2)), match.group(3), int(match.group(4)))

    async def callback(self, interaction: discord.Interaction):
        st = _state_from(interaction, self.guild_id, self.clone_id, "clans", self.clan, self.page)
        await _redraw(interaction, st, reload=True)


DYNAMIC_ITEMS = (XPTabButton, XPCloseButton, XPPingToggle, XPClanSelect, XPPageButton)


async def open_xp_wizard(interaction: discord.Interaction, guild_id: int, clone_id, tab: str = "boost") -> None:
    """Opens the wizard as a private message. Acknowledges first (the lookups can take a moment)."""
    await interaction.response.defer(ephemeral=True)
    st = _state_from(interaction, guild_id, clone_id, tab)
    await st.load()
    if st.tab == "clans":
        try:
            await st._load_clan_page()
        except Exception:
            logger.warning("[xp-wizard] clan lookup failed", exc_info=True)
    await interaction.followup.send(view=st.build(), ephemeral=True)
