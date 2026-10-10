# path: discord_bot/cogs/_views_scamshield_wizard.py

"""
/scamshield wizard (see discord_bot/cogs/scam_shield.py for the command).

One ephemeral panel for the server's staff (Manage Server / owner):

  Free     Turn on/off here · Allow domains · Check a link · Recent catches · How it works
  Premium  Strict Scam Shield (look-alike links, fake-subdomain tricks, "free nitro / airdrop" bait)
           Deep report (30-day breakdown: what, where, who, trend)

Free servers see the premium pitch on those two buttons (same pattern as Anti-raid Pro): the free shield keeps
working exactly as before. Strict mode only runs while premium is active; if premium lapses the saved choice is
kept but paused (see cogs/scam_shield.py `_strict_hit`).

Every control is a DynamicItem with guild_id/clone_id encoded in its custom_id, so the panel keeps working after a
bot restart. Registered in bot.py via DYNAMIC_ITEMS.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import List, Optional

import discord

from modules import antiraid_pro as pro          # is_premium: 30s cache, fails safe to "not premium"
from modules import scam_shield as ss
from modules import server_panel_protection as spp

logger = logging.getLogger(__name__)

KINDS = ("toggle", "allow", "check", "recent", "strict", "deep", "upgrade", "info")
_PAT = r"^scamwiz:(?P<kind>" + "|".join(KINDS) + r"):(?P<guild>\d+):(?P<clone>-|\d+)$"
_NO_PINGS = discord.AllowedMentions.none()


def _cid(kind: str, guild_id: int, clone_id) -> str:
    return f"scamwiz:{kind}:{guild_id}:{'-' if clone_id is None else clone_id}"


# ── state + rendering ─────────────────────────────────────────────────────

async def load_state(guild: discord.Guild, clone_id) -> dict:
    scam, premium = await asyncio.gather(spp.scam_state(guild.id, clone_id), pro.is_premium(guild.id, clone_id))
    return {"scam": scam, "premium": premium, "strict": ss.strict_on(guild.id, clone_id)}


def status_lines(guild: discord.Guild, st: dict) -> List[str]:
    cfg = st["scam"].get("cfg", {})
    on = bool(cfg.get("enabled", True))
    allowed = cfg.get("allowed_domains") or []
    lines = [f"Status here: {'🟢 **on**' if on else '🔴 **off**'} · **{st['scam'].get('caught', 0)}** scam message(s) caught"]
    if not st["scam"].get("global_on", True):
        lines.append("⏸️ The bot owner has switched Scam Shield off for every server for now.")
    me = guild.me
    if me is not None and not me.guild_permissions.manage_messages:
        lines.append("⚠️ **Needs attention:** I'm missing the **Manage Messages** permission, so I can't delete scams. "
                     "Enable it on my role.")
    lines.append(f"Allowed domains: **{len(allowed)}**" + ("" if allowed else " (add one if a real link keeps getting deleted)"))
    if st["premium"]:
        lines.append(f"💎 Strict Scam Shield: {'🟢 **on**' if st['strict'] else '⚪ off'} · Deep reports: ✅ unlocked")
    return lines


PITCH = (
    "**💎 Upgrade to Premium to unlock**\n"
    "• 🔒 **Strict Scam Shield** — also catches look-alike links (like `discord-gift.xyz`), fake "
    "`discord.com.evil.xyz` tricks and \"free nitro / airdrop\" bait\n"
    "• 🔒 **Deep reports** — a 30-day breakdown of what was caught, where, by whom, and the daily trend"
)

HOW_IT_WORKS = (
    "🛡️ **How Scam Shield works**\n"
    "• It deletes known scam messages (casino / giveaway bait, fake payout screenshots, known scam sites) in every "
    "server the bot is in, and posts a flag in your mod-log channel.\n"
    "• Staff (Manage Messages, Manage Server, Admin) are never checked.\n"
    "• **Allow domains** is for real links that got deleted by mistake.\n"
    "• **Check a link** tells you what Scam Shield would do with a link or message, without deleting anything.\n"
    "• 💎 **Premium** adds Strict mode and Deep reports. The free shield keeps working either way."
)


def build_panel(guild: discord.Guild, clone_id, st: dict, note: str = "") -> discord.ui.LayoutView:
    cfg = st["scam"].get("cfg", {})
    on = bool(cfg.get("enabled", True))
    premium = st["premium"]
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.green() if on else discord.Color.orange())
    if note:      # a Components v2 message can't carry plain `content`, so the note lives inside the panel
        container.add_item(discord.ui.TextDisplay(note))
    container.add_item(discord.ui.TextDisplay("### 🛡️ Scam Shield\n" + "\n".join(status_lines(guild, st))))
    container.add_item(discord.ui.Separator())
    if not premium:
        container.add_item(discord.ui.TextDisplay(PITCH))
        container.add_item(discord.ui.Separator())

    free = discord.ui.ActionRow()
    for kind in ("toggle", "allow", "check", "recent"):
        free.add_item(WizButton(kind, guild.id, clone_id, on=on))
    container.add_item(free)

    extra = discord.ui.ActionRow()
    extra.add_item(WizButton("strict", guild.id, clone_id, premium=premium, strict=st["strict"]))
    extra.add_item(WizButton("deep", guild.id, clone_id, premium=premium))
    extra.add_item(WizButton("info" if premium else "upgrade", guild.id, clone_id))
    if not premium:
        extra.add_item(WizButton("info", guild.id, clone_id))
    container.add_item(extra)
    view.add_item(container)
    return view


async def render(guild: discord.Guild, clone_id, note: str = "") -> discord.ui.LayoutView:
    return build_panel(guild, clone_id, await load_state(guild, clone_id), note)


async def open_wizard(interaction: discord.Interaction, guild: discord.Guild, clone_id, note: str = "") -> None:
    """Shared entry point. Call AFTER interaction.response.defer(ephemeral=True) and after the permission check."""
    await interaction.followup.send(view=await render(guild, clone_id, note), ephemeral=True)


# ── reports (pure text builders, easy to test) ────────────────────────────

_BARS = "▁▂▃▄▅▆▇█"


def spark(counts: List[int]) -> str:
    top = max(counts) if counts else 0
    if top <= 0:
        return _BARS[0] * len(counts)
    return "".join(_BARS[min(len(_BARS) - 1, round(c / top * (len(_BARS) - 1)))] if c else _BARS[0] for c in counts)


def trend(daily, days: int = 14, today: Optional[date] = None) -> List[int]:
    """Per-day counts for the last `days` days (oldest first), zeros filled in."""
    today = today or date.today()
    by_day = {d: n for d, n in daily}
    return [int(by_day.get(today - timedelta(days=i), 0)) for i in range(days - 1, -1, -1)]


def recent_text(rows: List[dict]) -> str:
    if not rows:
        return "🛡️ **Recent catches**\nNothing has been caught in this server yet. 🎉"
    lines = ["🛡️ **Recent catches** (newest first)"]
    for r in rows:
        when = discord.utils.format_dt(r["created_at"], "R") if r.get("created_at") else "?"
        what = discord.utils.escape_markdown(str(r.get("matched") or r.get("kind")))[:60]
        lines.append(f"• {when} · <@{r['user_id']}> · {r['kind']}: {what} · "
                     f"{'🗑️ deleted' if r.get('deleted') else '⚠️ not deleted'}")
    return "\n".join(lines)[:1900]


def deep_text(rep: dict) -> str:
    if not rep:
        return "⚠️ I couldn't build the report right now. Try again in a moment."
    days = rep["days"]
    if not rep["total"]:
        return f"📊 **Deep report · last {days} days**\nNo scam messages were caught here. Quiet is good. 🎉"
    last = discord.utils.format_dt(rep["last"], "R") if rep.get("last") else "?"
    counts = trend(rep["daily"])
    lines = [
        f"📊 **Deep report · last {days} days**",
        f"**{rep['total']}** caught ({rep['deleted']} deleted) from **{rep['users']}** account(s) · last one {last}",
        f"Trend (14 days): `{spark(counts)}` peak **{max(counts)}**/day",
        "**What matched:** " + ", ".join(f"{discord.utils.escape_markdown(str(m))[:40]} ×{n}" for m, n in rep["matched"]),
        "**By type:** " + ", ".join(f"{k} ×{n}" for k, n in rep["kinds"]),
        "**Busiest channels:** " + ", ".join(f"<#{c}> ×{n}" for c, n in rep["channels"] if c),
        "**Most active accounts:** " + ", ".join(f"<@{u}> ×{n}" for u, n in rep["users_top"]),
        "-# Logged catches are capped at one per account per 30 seconds, so real numbers can be higher.",
    ]
    return "\n".join(lines)[:1950]


# ── shared helpers ────────────────────────────────────────────────────────

async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=_NO_PINGS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=_NO_PINGS)


async def _auth(interaction: discord.Interaction, guild_id: int) -> Optional[discord.Guild]:
    """Resolves the guild from the custom_id and checks Manage Server / owner. None after telling the user why."""
    guild = interaction.client.get_guild(guild_id)
    if guild is None:
        await _reply(interaction, "I'm not in that server anymore.")
        return None
    member = guild.get_member(interaction.user.id)
    if member is None or not (member.guild_permissions.manage_guild or member.id == guild.owner_id):
        await _reply(interaction, "You need the **Manage Server** permission to use Scam Shield settings.")
        return None
    return guild


async def _pitch(interaction: discord.Interaction, guild: discord.Guild, clone_id, what: str) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        f"🔒 **{what}** is a premium extra. The free Scam Shield keeps protecting your server either way. "
        "Here's how to unlock it 👇", ephemeral=True)
    from discord_bot.cogs._views_premium import send_premium_pitch
    await send_premium_pitch(interaction, guild.id, clone_id)


async def _rerender(interaction: discord.Interaction, guild: discord.Guild, clone_id, note: str = "") -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    await interaction.edit_original_response(view=await render(guild, clone_id, note))


# ── modals ────────────────────────────────────────────────────────────────

class AllowModal(discord.ui.Modal):
    domains = discord.ui.TextInput(label="Domains to allow (one per line)", style=discord.TextStyle.paragraph,
                                   max_length=1000, placeholder="example.com\nmy-site.org")

    def __init__(self, guild_id: int, clone_id):
        super().__init__(title="Allow domains", timeout=300)
        self.guild_id, self.clone_id = guild_id, clone_id

    async def on_submit(self, interaction: discord.Interaction):
        guild = await _auth(interaction, self.guild_id)
        if guild is None:
            return
        from database import db
        await interaction.response.defer()
        current = (await db.get_scam_shield_guild(guild.id, clone_id=self.clone_id)).get("allowed_domains") or []
        added, rejected, final = spp.clean_domains(str(self.domains.value), current)
        if added:
            await spp.set_allowed_domains(guild.id, self.clone_id, interaction.user.id, final)
        await interaction.edit_original_response(view=await render(guild, self.clone_id))
        note = []
        if added:
            note.append("✅ Allowed: " + ", ".join(f"`{d}`" for d in added))
        if rejected:
            note.append("⚠️ Skipped (not a valid domain, or the 50-domain limit): " + ", ".join(f"`{r}`" for r in rejected))
        if note:
            await interaction.followup.send("\n".join(note), ephemeral=True)


class CheckModal(discord.ui.Modal):
    text = discord.ui.TextInput(label="Paste a link or message", style=discord.TextStyle.paragraph,
                                max_length=1500, placeholder="https://…")

    def __init__(self, guild_id: int, clone_id):
        super().__init__(title="Check a link", timeout=300)
        self.guild_id, self.clone_id = guild_id, clone_id

    async def on_submit(self, interaction: discord.Interaction):
        guild = await _auth(interaction, self.guild_id)
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        from database import db
        cfg = await db.get_scam_shield_guild(guild.id, clone_id=self.clone_id)
        strict = ss.strict_on(guild.id, self.clone_id) and await pro.is_premium(guild.id, self.clone_id)
        hit = ss.check_text(str(self.text.value), tuple(d.lower() for d in cfg.get("allowed_domains") or ()), strict)
        if hit is None:
            msg = "✅ **Not flagged.** Scam Shield would leave this alone" + (" (strict mode included)." if strict else ".")
        else:
            msg = (f"🛑 **Would be caught** ({hit[0]}: {discord.utils.escape_markdown(str(hit[1]))[:120]}). "
                   "If this is a real link, use **Allow domains**.")
        await interaction.followup.send(msg, ephemeral=True, allowed_mentions=_NO_PINGS)


# ── buttons ───────────────────────────────────────────────────────────────

class WizButton(discord.ui.DynamicItem[discord.ui.Button], template=_PAT):
    def __init__(self, kind: str, guild_id: int, clone_id, on: bool = True, premium: bool = True, strict: bool = False):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        label, style, emoji = {
            "toggle": ("Turn off here" if on else "Turn on here", discord.ButtonStyle.danger if on else discord.ButtonStyle.success, None),
            "allow": ("Allow domains", discord.ButtonStyle.primary, "➕"),
            "check": ("Check a link", discord.ButtonStyle.secondary, "🔎"),
            "recent": ("Recent catches", discord.ButtonStyle.secondary, "📜"),
            "strict": (("Strict: on — turn off" if strict else "Strict mode") if premium else "Strict mode 🔒",
                       (discord.ButtonStyle.danger if strict else discord.ButtonStyle.success) if premium
                       else discord.ButtonStyle.secondary, "💎" if premium else None),
            "deep": ("Deep report" if premium else "Deep report 🔒",
                     discord.ButtonStyle.success if premium else discord.ButtonStyle.secondary, "📊" if premium else None),
            "upgrade": ("Upgrade to Premium", discord.ButtonStyle.primary, "💎"),
            "info": ("How it works", discord.ButtonStyle.secondary, "❓"),
        }[kind]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        clone = match.group("clone")
        return cls(match.group("kind"), int(match.group("guild")), None if clone == "-" else int(clone))

    async def callback(self, interaction: discord.Interaction):
        guild = await _auth(interaction, self.guild_id)
        if guild is None:
            return
        try:
            await getattr(self, f"_do_{self.kind}")(interaction, guild)
        except Exception:
            logger.exception("[scamshield-wizard] %s failed in guild %s", self.kind, self.guild_id)
            await _reply(interaction, "⚠️ Something went wrong. Please try again.")

    async def _do_toggle(self, interaction, guild):
        await interaction.response.defer()
        cfg = (await spp.scam_state(guild.id, self.clone_id))["cfg"]
        await spp.set_scam_enabled(guild.id, self.clone_id, interaction.user.id, not bool(cfg.get("enabled", True)))
        await _rerender(interaction, guild, self.clone_id)

    async def _do_allow(self, interaction, guild):
        await interaction.response.send_modal(AllowModal(guild.id, self.clone_id))

    async def _do_check(self, interaction, guild):
        await interaction.response.send_modal(CheckModal(guild.id, self.clone_id))

    async def _do_recent(self, interaction, guild):
        await interaction.response.defer(ephemeral=True)
        rows = await ss.recent_guild_hits(guild.id, self.clone_id, limit=5)
        await interaction.followup.send(recent_text(rows), ephemeral=True, allowed_mentions=_NO_PINGS)

    async def _do_info(self, interaction, guild):
        await _reply(interaction, HOW_IT_WORKS)

    async def _do_upgrade(self, interaction, guild):
        if await pro.is_premium(guild.id, self.clone_id):
            await _reply(interaction, "💎 This server already has premium. Strict mode and Deep reports are unlocked.")
            return
        await _pitch(interaction, guild, self.clone_id, "Strict Scam Shield and Deep reports")

    async def _do_strict(self, interaction, guild):
        if not await pro.is_premium(guild.id, self.clone_id):
            await _pitch(interaction, guild, self.clone_id, "Strict Scam Shield")
            return
        await interaction.response.defer()
        turn_on = not ss.strict_on(guild.id, self.clone_id)
        await ss.set_strict(guild.id, self.clone_id, turn_on)
        from modules.server_panel import record_change
        await record_change(guild.id, self.clone_id, interaction.user.id, "scam_shield.strict", not turn_on, turn_on)
        await _rerender(interaction, guild, self.clone_id)
        await interaction.followup.send(
            ("🟢 **Strict mode is on.** It also catches look-alike links, fake-subdomain tricks and \"free nitro / "
             "airdrop\" bait. It is more aggressive than the normal shield; use **Check a link** to test a link and "
             "**Allow domains** for real sites it blocks.") if turn_on else
            "⚪ Strict mode is off. The normal Scam Shield is still protecting this server.", ephemeral=True)

    async def _do_deep(self, interaction, guild):
        if not await pro.is_premium(guild.id, self.clone_id):
            await _pitch(interaction, guild, self.clone_id, "Deep reports")
            return
        await interaction.response.defer(ephemeral=True)
        rep = await ss.deep_report(guild.id, self.clone_id)
        await interaction.followup.send(deep_text(rep), ephemeral=True, allowed_mentions=_NO_PINGS)


DYNAMIC_ITEMS = (WizButton,)
