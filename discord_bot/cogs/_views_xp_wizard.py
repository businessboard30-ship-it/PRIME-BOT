# path: discord_bot/cogs/_views_xp_wizard.py

"""
The leaderboard's "XP & Clans" wizard: ONE private message with four tabs that edits itself in place, instead of a loose
row of buttons that each pop open another message.

  ⚡ Boost XP     your own boost: the Level Pass and the multi-boost packs, bought right here
  🔔 Pings        level-up pings on or off, with the current state shown
  🏰 Clans        pick a clan and page through its members
  🌍 Server boost buy a server-wide XP boost for everyone

A summary of your boost, the server boost and your ping setting sits at the top of every tab. Every purchase goes through
the same checkout code as before (_views_leveling_boost._start_boost_checkout and BuyBoostSelect), so prices, products
and payment modes are unchanged. Nothing here writes anything except the ping toggle (db.toggle_level_ping_optout).
"""

from __future__ import annotations

import asyncio
import logging
import time
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


class XPWizardView(discord.ui.View):
    def __init__(self, guild_id: int, clone_id, user_id: int, tab: str = "boost"):
        super().__init__(timeout=600)
        self.guild_id, self.clone_id, self.user_id = guild_id, clone_id, user_id
        self.tab = tab if tab in {t[0] for t in TABS} else "boost"
        self.boost = self.server_boost = None
        self.muted = False
        self.bundles: dict = {}
        self.offer_pass = False
        self.clan: Optional[str] = None
        self.page = 0
        self.clan_total = 0
        self.clan_lines: list = []
        self.guild = None
        self.message: Optional[discord.Message] = None

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

    # ---- drawing ----
    def embed(self) -> discord.Embed:
        e = discord.Embed(title="⚡ XP & Clans", color=PINK)
        summary = [_boost_line(self.boost, "Your XP boost"), _boost_line(self.server_boost, "Server boost"),
                   f"**Level-up pings:** {'🔕 off' if self.muted else '🔔 on'}"]
        e.description = "\n".join(summary)
        name = {t[0]: f"{t[2]} {t[1]}" for t in TABS}[self.tab]
        if self.tab == "boost":
            e.add_field(name=name, value=self._boost_text(), inline=False)
        elif self.tab == "pings":
            e.add_field(name=name, value=("🔕 **Off.** Level-up announcements still appear, but I won't @ping you." if self.muted else
                                          "🔔 **On.** I'll @ping you when you level up."), inline=False)
        elif self.tab == "clans":
            if not self.clan:
                e.add_field(name=name, value="Pick a clan below to see its members.", inline=False)
            else:
                from modules import clan_cards
                body = "\n".join(self.clan_lines) or "Nobody's been assigned to this clan here yet."
                e.add_field(name=f"{name} · {clan_cards.get_clan_label(self.clan)} · {self.clan_total} member"
                                 f"{'' if self.clan_total == 1 else 's'}", value=body[:1000], inline=False)
        else:
            tiers = [f"• **{t['label']}** · ${t['fee_usd']:g} · {t['multiplier']:g}x XP for everyone, {t['duration_hours']}h"
                     for k, t in app_config.XP_SERVER_BOOST_TIERS.items()]
            e.add_field(name=name, value="Everyone in the server levels faster for a while.\n" + "\n".join(tiers), inline=False)
        e.set_footer(text="Only you can see this.")
        return e

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

    def build(self) -> "XPWizardView":
        """Rebuild the controls for the current tab: tabs on row 0, the tab's own controls below."""
        self.clear_items()
        for key, label, emoji in TABS:
            b = discord.ui.Button(label=label, emoji=emoji, row=0,
                                  style=discord.ButtonStyle.primary if key == self.tab else discord.ButtonStyle.secondary)
            b.callback = self._tab_callback(key)
            self.add_item(b)
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, row=0)
        close.callback = self._close
        self.add_item(close)
        if self.tab == "boost":
            self._add_buy_buttons()
        elif self.tab == "pings":
            t = discord.ui.Button(label="Turn pings ON" if self.muted else "Turn pings OFF", emoji="🔔" if self.muted else "🔕",
                                  style=discord.ButtonStyle.success if self.muted else discord.ButtonStyle.danger, row=1)
            t.callback = self._toggle_pings
            self.add_item(t)
        elif self.tab == "clans":
            from modules.clan_cards import CLAN_CARDS
            sel = discord.ui.Select(placeholder="Choose a clan…", row=1, options=[
                discord.SelectOption(label=label, value=fn, default=(fn == self.clan)) for fn, label, *_ in CLAN_CARDS])
            sel.callback = self._pick_clan
            self.add_item(sel)
            if self.clan:
                pages = max(1, -(-self.clan_total // CLAN_PAGE))
                for label, delta, off in (("◀", -1, self.page <= 0), ("▶", 1, self.page >= pages - 1)):
                    b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off, row=2)
                    b.callback = self._page_callback(delta)
                    self.add_item(b)
                self.add_item(discord.ui.Button(label=f"{self.page + 1}/{pages}", disabled=True, row=2,
                                                style=discord.ButtonStyle.secondary))
        else:
            from discord_bot.cogs._views_leveling_wallet import BuyBoostSelect
            sel = BuyBoostSelect(self.guild_id, self.clone_id)
            sel.item.row = 1
            self.add_item(sel)
        return self

    def _add_buy_buttons(self) -> None:
        from discord_bot.cogs import _views_leveling_boost as boost_mod
        items = []
        if self.offer_pass:
            items.append(("level_pass", f"Level Pass · ${app_config.LEVEL_PASS_FEE_USD:g}", "🎫"))
        items += [(k, f"{b['label']} · ${b['fee_usd']:g}", "⚡") for k, b in self.bundles.items()]
        for pt, label, emoji in items[:10]:
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.success, row=1 if len(self.children) < 9 else 2)

            async def _cb(interaction: discord.Interaction, _pt=pt):
                await interaction.response.defer(ephemeral=True, thinking=True)
                await boost_mod._start_boost_checkout(interaction, _pt, self.guild_id)
            btn.callback = _cb
            self.add_item(btn)

    # ---- callbacks ----
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel belongs to someone else. Tap **XP & Clans** to open your own.",
                                                    ephemeral=True)
            return False
        return True

    def _tab_callback(self, key: str):
        async def cb(interaction: discord.Interaction):
            self.tab = key
            await self._redraw(interaction, refresh=True)
        return cb

    def _page_callback(self, delta: int):
        async def cb(interaction: discord.Interaction):
            self.page = max(0, self.page + delta)
            await self._redraw(interaction)
        return cb

    async def _pick_clan(self, interaction: discord.Interaction):
        self.clan = interaction.data["values"][0]
        self.page = 0
        await self._redraw(interaction)

    async def _toggle_pings(self, interaction: discord.Interaction):
        self.muted = await db.toggle_level_ping_optout(self.guild_id, interaction.user.id)
        await self._redraw(interaction, reload=False)

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(embed=self.embed(), view=None)

    async def _redraw(self, interaction: discord.Interaction, refresh: bool = False, reload: bool = True) -> None:
        self.guild = interaction.guild or self.guild
        try:
            if refresh:
                await self.load()                       # a boost may have just been bought: show the fresh state
            if self.tab == "clans" and reload:
                await self._load_clan_page()
        except Exception:
            logger.warning("[xp-wizard] redraw lookup failed", exc_info=True)
        await interaction.response.edit_message(embed=self.embed(), view=self.build())

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def open_xp_wizard(interaction: discord.Interaction, guild_id: int, clone_id, tab: str = "boost") -> None:
    """Opens the wizard as a private message. Acknowledges first (the lookups can take a moment)."""
    await interaction.response.defer(ephemeral=True)
    view = XPWizardView(guild_id, clone_id, interaction.user.id, tab)
    view.guild = interaction.guild
    await view.load()
    if view.tab == "clans":
        try:
            await view._load_clan_page()
        except Exception:
            logger.warning("[xp-wizard] clan lookup failed", exc_info=True)
    view.message = await interaction.followup.send(embed=view.embed(), view=view.build(), ephemeral=True, wait=True)
