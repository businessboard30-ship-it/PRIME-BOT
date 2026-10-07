# path: discord_bot/cogs/_views_antiraid_pro.py

"""
Anti-raid Pro UI (premium extras for discord_bot/cogs/antiraid.py).

  * Quarantine review buttons — Ban all / Release all / Review one-by-one. They are attached to the
    alert and to the raid report, and also reachable from /antiraid. They act ONLY on people that
    anti-raid itself quarantined (reason starts with "[anti-raid]"), never on staff-made quarantines.
    Ban all asks for a confirmation tap first.
  * Profile-filter settings — an ephemeral panel (account age, default avatar, suspicious name,
    action). Premium only: free servers get the premium pitch, the same pattern as the honeypot's
    `need_premium` gate.

Every control is a DynamicItem with guild_id/clone_id (and the user id for one-by-one review) encoded
in its custom_id, so it keeps working after a bot restart. Registered in bot.py via DYNAMIC_ITEMS.
Review/ban/release stay available after premium lapses so staff can always clean up; only the
settings and the pitch are gated.
"""

from __future__ import annotations

import logging
import re

import discord

from database import db
from modules import antiraid_pro as pro

logger = logging.getLogger(__name__)


def _cid(kind: str, guild_id: int, clone_id, uid: int | None = None) -> str:
    base = f"arpro:{kind}:{guild_id}:{'-' if clone_id is None else clone_id}"
    return base if uid is None else f"{base}:{uid}"


def _ids(match: "re.Match"):
    clone = match.group("clone")
    return int(match.group("guild")), (None if clone == "-" else int(clone))


_BASE = r":(?P<guild>\d+):(?P<clone>-|\d+)"


# ── shared helpers ────────────────────────────────────────────────────────

async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _auth(interaction: discord.Interaction, guild_id: int, need: str, premium: bool = False):
    """Resolves the guild from the custom_id and checks the right permission.
    need: 'manage' (Manage Server), 'review' (Ban Members / Manage Server), 'ban' (Ban Members).
    With premium=True a free server gets the premium pitch instead. Returns the guild or None."""
    guild = interaction.client.get_guild(guild_id)
    if guild is None:
        await _reply(interaction, "I'm not in that server anymore.")
        return None
    member = guild.get_member(interaction.user.id)
    is_owner = member is not None and member.id == guild.owner_id
    if need == "manage":
        ok = is_owner or bool(member and member.guild_permissions.manage_guild)
        why = "You need the **Manage Server** permission to use this."
    elif need == "ban":
        ok = is_owner or pro.can_ban(member)
        why = "You need the **Ban Members** permission to ban people."
    else:
        ok = is_owner or pro.can_review(member)
        why = "You need **Ban Members** or **Manage Server** to review quarantined people."
    if not ok:
        await _reply(interaction, why)
        return None
    clone_id = getattr(interaction.client, "clone_id", None)
    if premium and not await pro.is_premium(guild_id, clone_id):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.followup.send(
            "🛡️ **Anti-raid Pro** (profile filter, raid reports, quarantine & review) is a premium "
            "extra — the free anti-raid keeps working. Here's how to unlock it 👇", ephemeral=True)
        await send_premium_pitch(interaction, guild_id, clone_id)
        return None
    return guild


async def _save(interaction, guild: discord.Guild, clone_id, **fields) -> dict:
    """Audited write (same trail as every other panel setting) + anti-raid cache refresh."""
    from modules import server_panel as sp
    from discord_bot.cogs import antiraid
    await sp.set_antiraid(guild.id, clone_id, interaction.user.id, **fields)
    antiraid._invalidate(guild.id, clone_id)
    return await db.get_antiraid_config(guild.id, clone_id=clone_id)


# ── review / action views ─────────────────────────────────────────────────

def action_view(guild_id: int, clone_id) -> discord.ui.View:
    """Ban all / Release all / Review one-by-one, for attaching to alerts and reports."""
    view = discord.ui.View(timeout=None)
    for kind in ("qban", "qrel", "qreview"):
        view.add_item(ProButton(kind, guild_id, clone_id))
    return view


def review_view(guild_id: int, clone_id, uid: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for kind in ("rban", "rrel", "rskip", "rstop"):
        view.add_item(ReviewButton(kind, guild_id, clone_id, uid))
    return view


async def _show_next(interaction: discord.Interaction, guild: discord.Guild, clone_id,
                     before: list, uid: int, note: str) -> None:
    """After acting on / skipping `uid`: show the next person from the list as it was before."""
    now_ids = {int(r["user_id"]) for r in await pro.raid_quarantined(guild, clone_id)}
    ids = [int(r["user_id"]) for r in before]
    start = ids.index(uid) + 1 if uid in ids else 0
    for i in range(start, len(before)):
        if int(before[i]["user_id"]) in now_ids:
            row = before[i]
            await interaction.edit_original_response(
                content=note, embed=pro.review_embed(guild, row, i + 1, len(before)),
                view=review_view(guild.id, clone_id, int(row["user_id"])))
            return
    left = len(now_ids)
    await interaction.edit_original_response(
        content=(note + "\n" if note else "") + f"✅ Review finished. **{left}** still quarantined"
        + (" (the ones you skipped)." if left else "."),
        embed=None, view=None, allowed_mentions=discord.AllowedMentions.none())


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"^arpro:(?P<kind>rban|rrel|rskip|rstop)" + _BASE + r":(?P<uid>\d+)$"):
    _LOOK = {
        "rban": ("Ban", discord.ButtonStyle.danger, "🔨"),
        "rrel": ("Release", discord.ButtonStyle.success, "🔓"),
        "rskip": ("Skip", discord.ButtonStyle.secondary, "⏭️"),
        "rstop": ("Stop", discord.ButtonStyle.secondary, "⏹️"),
    }

    def __init__(self, kind: str, guild_id: int, clone_id, uid: int):
        self.kind, self.guild_id, self.clone_id, self.uid = kind, guild_id, clone_id, uid
        label, style, emoji = self._LOOK[kind]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=_cid(kind, guild_id, clone_id, uid)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id, int(match.group("uid")))

    async def callback(self, interaction: discord.Interaction):
        need = "ban" if self.kind == "rban" else "review"
        guild = await _auth(interaction, self.guild_id, need)
        if guild is None:
            return
        await interaction.response.defer()
        before = await pro.raid_quarantined(guild, self.clone_id)
        if self.kind == "rstop":
            await interaction.edit_original_response(
                content=f"⏹️ Review stopped. **{len(before)}** still quarantined.", embed=None, view=None)
            return
        note = ""
        if self.kind == "rban":
            _ok, note = await pro.ban_suspect(guild, self.clone_id, interaction.user, self.uid)
        elif self.kind == "rrel":
            _ok, note = await pro.release_suspect(guild, self.clone_id, interaction.user.id, self.uid)
        await _show_next(interaction, guild, self.clone_id, before, self.uid, note)


class ProButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"^arpro:(?P<kind>qban|qbanyes|qcancel|qrel|qreview|filter|upgrade|favatar|fname)" + _BASE + r"$"):
    _LOOK = {
        "qban": ("Ban all", discord.ButtonStyle.danger, "🔨"),
        "qbanyes": ("Yes, ban them all", discord.ButtonStyle.danger, "🔨"),
        "qcancel": ("Cancel", discord.ButtonStyle.secondary, "✖️"),
        "qrel": ("Release all", discord.ButtonStyle.success, "🔓"),
        "qreview": ("Review one-by-one", discord.ButtonStyle.primary, "🔎"),
        "filter": ("Profile filter", discord.ButtonStyle.primary, "🪪"),
        "upgrade": ("Unlock Anti-raid Pro", discord.ButtonStyle.success, "💎"),
        "favatar": ("Default avatar", discord.ButtonStyle.secondary, "🖼️"),
        "fname": ("Suspicious name", discord.ButtonStyle.secondary, "🔤"),
    }

    def __init__(self, kind: str, guild_id: int, clone_id, label: str | None = None,
                 style: discord.ButtonStyle | None = None):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        d_label, d_style, emoji = self._LOOK[kind]
        super().__init__(discord.ui.Button(label=label or d_label, style=style or d_style, emoji=emoji,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        k, cid = self.kind, self.clone_id

        if k == "upgrade":
            guild = await _auth(interaction, self.guild_id, "manage")
            if guild is None:
                return
            await interaction.response.defer(ephemeral=True)
            from discord_bot.cogs._views_premium import send_premium_pitch
            await send_premium_pitch(interaction, self.guild_id, cid)

        elif k == "filter":
            guild = await _auth(interaction, self.guild_id, "manage", premium=True)
            if guild is None:
                return
            await interaction.response.defer(ephemeral=True)
            cfg = await db.get_antiraid_config(guild.id, clone_id=cid)
            await interaction.followup.send(view=build_filter_panel(guild, cid, cfg), ephemeral=True)

        elif k in ("favatar", "fname"):
            guild = await _auth(interaction, self.guild_id, "manage", premium=True)
            if guild is None:
                return
            await interaction.response.defer()
            cfg = await db.get_antiraid_config(guild.id, clone_id=cid)
            field = "filter_default_avatar" if k == "favatar" else "filter_suspicious_name"
            cfg = await _save(interaction, guild, cid, **{field: not cfg.get(field)})
            await interaction.edit_original_response(view=build_filter_panel(guild, cid, cfg))

        elif k == "qreview":
            guild = await _auth(interaction, self.guild_id, "review")
            if guild is None:
                return
            await interaction.response.defer(ephemeral=True)
            rows = await pro.raid_quarantined(guild, cid)
            if not rows:
                await interaction.followup.send("Nobody is waiting for review. 🎉", ephemeral=True)
                return
            await interaction.followup.send(
                embed=pro.review_embed(guild, rows[0], 1, len(rows)),
                view=review_view(guild.id, cid, int(rows[0]["user_id"])), ephemeral=True)

        elif k == "qrel":
            guild = await _auth(interaction, self.guild_id, "review")
            if guild is None:
                return
            await interaction.response.defer(ephemeral=True)
            msg = await pro.release_all(guild, cid, interaction.user.id)
            await interaction.followup.send(msg, ephemeral=True)

        elif k == "qban":
            guild = await _auth(interaction, self.guild_id, "ban")
            if guild is None:
                return
            rows = await pro.raid_quarantined(guild, cid)
            if not rows:
                await _reply(interaction, "Nobody is waiting for review. 🎉")
                return
            confirm = discord.ui.View(timeout=None)
            confirm.add_item(ProButton("qbanyes", guild.id, cid))
            confirm.add_item(ProButton("qcancel", guild.id, cid))
            await interaction.response.send_message(
                f"⚠️ Ban **{len(rows)}** quarantined account(s)? This can't be undone. "
                "Use **Review one-by-one** if you want to check them first.",
                view=confirm, ephemeral=True)

        elif k == "qbanyes":
            guild = await _auth(interaction, self.guild_id, "ban")
            if guild is None:
                return
            await interaction.response.defer()
            msg = await pro.ban_all(guild, cid, interaction.user)
            await interaction.edit_original_response(content=msg, view=None)

        elif k == "qcancel":
            await interaction.response.edit_message(content="Cancelled — nobody was banned.", view=None)


# ── profile-filter settings panel ─────────────────────────────────────────

class ProSelect(discord.ui.DynamicItem[discord.ui.Select],
                template=r"^arpro:(?P<kind>fage|faction)" + _BASE + r"$"):
    def __init__(self, kind: str, guild_id: int, clone_id, current: str = ""):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        if kind == "fage":
            placeholder = "Flag accounts younger than…"
            options = [discord.SelectOption(
                label="Don't check account age" if d == 0 else f"Younger than {d} day{'s' if d != 1 else ''}",
                value=str(d), default=(str(d) == current)) for d in pro.FILTER_AGE_CHOICES]
        else:
            placeholder = "What should I do with a flagged joiner?"
            options = [discord.SelectOption(label=v[0], value=k2, emoji=v[1], description=v[2],
                                            default=(k2 == current)) for k2, v in pro.FILTER_ACTIONS.items()]
        super().__init__(discord.ui.Select(placeholder=placeholder, options=options, min_values=1, max_values=1,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        guild = await _auth(interaction, self.guild_id, "manage", premium=True)
        if guild is None:
            return
        value = self.item.values[0]
        if self.kind == "fage":
            if value not in {str(d) for d in pro.FILTER_AGE_CHOICES}:
                return
            fields = {"filter_age_days": int(value)}
        else:
            if value not in pro.FILTER_ACTIONS:
                return
            fields = {"filter_action": value}
        await interaction.response.defer()
        cfg = await _save(interaction, guild, self.clone_id, **fields)
        await interaction.edit_original_response(view=build_filter_panel(guild, self.clone_id, cfg))
        if self.kind == "faction" and value == "quarantine":
            problem = await quarantine_setup_problem(guild, self.clone_id)
            if problem:
                await interaction.followup.send(problem, ephemeral=True)


async def quarantine_setup_problem(guild: discord.Guild, clone_id) -> str | None:
    """Why 'quarantine' can't work yet (no role picked / role unusable), or None."""
    from modules import server_panel_quarantine as spq
    role_id = await db.get_quarantine_role(guild.id, clone_id)
    if not role_id:
        return ("Heads up — there's **no quarantine role** yet, so I can't quarantine anyone. Set one in "
                "`/serversetup` → Moderation → Quarantine & lockdown. Until then I'll only flag.")
    problem = spq.role_problem(guild, guild.get_role(int(role_id)))
    return f"Heads up — quarantine isn't ready: {problem}" if problem else None


def build_filter_panel(guild: discord.Guild, clone_id, cfg: dict, note: str = "") -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())
    if note:
        container.add_item(discord.ui.TextDisplay(note))
    lines = [
        "### 🪪 Profile filter — Anti-raid Pro 💎",
        f"**Now:** {pro.filter_summary(cfg)}",
        "",
        "Checks every new joiner (staff and bots are never touched). Pick what counts as suspicious, "
        "then what to do about it. **Flag only** is the safe place to start.",
    ]
    if not cfg.get("enabled"):
        lines.append("\n⚠️ Anti-raid is **paused** — the filter only runs while it's on (turn it on in `/antiraid`).")
    container.add_item(discord.ui.TextDisplay("\n".join(lines)))
    container.add_item(discord.ui.Separator())

    row = discord.ui.ActionRow()
    row.add_item(ProSelect("fage", guild.id, clone_id, str(int(cfg.get("filter_age_days") or 0))))
    container.add_item(row)
    row = discord.ui.ActionRow()
    row.add_item(ProSelect("faction", guild.id, clone_id, cfg.get("filter_action") or pro.DEFAULT_FILTER_ACTION))
    container.add_item(row)

    on, off = discord.ButtonStyle.success, discord.ButtonStyle.secondary
    row = discord.ui.ActionRow()
    row.add_item(ProButton("favatar", guild.id, clone_id,
                           label="Default avatar: ON" if cfg.get("filter_default_avatar") else "Default avatar: off",
                           style=on if cfg.get("filter_default_avatar") else off))
    row.add_item(ProButton("fname", guild.id, clone_id,
                           label="Suspicious name: ON" if cfg.get("filter_suspicious_name") else "Suspicious name: off",
                           style=on if cfg.get("filter_suspicious_name") else off))
    container.add_item(row)
    view.add_item(container)
    return view


DYNAMIC_ITEMS = (ReviewButton, ProButton, ProSelect)
