# path: discord_bot/cogs/_views_ticket_wizard.py

"""
Bumper-style multi-step setup wizard for /ticket setup.

Free tier: panel channel, support role, ticket category, welcome message.
Premium tier (💎): multiple ticket categories, custom panel buttons,
auto-close on inactivity, transcript logging, custom close message.

Premium items are visible to all admins but locked — tapping them shows
the Go Premium pitch instead of the setting if the guild isn't subscribed.
"""

import json
import re

import discord

import config
from database import db
from discord_bot.cogs._views_shared import check_wizard_access

MAX_MESSAGE_LEN = 300
MAX_CATEGORIES = 5
MAX_CUSTOM_BUTTONS = 3


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _is_premium(guild_id: int, clone_id) -> bool:
    try:
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False


def _lock(label: str, premium: bool) -> str:
    return label if premium else f"💎 {label}"


def _status_color(config_row: dict) -> discord.Color:
    return discord.Color.green() if config_row.get("panel_message_id") else discord.Color.blurple()


def render_status_lines(cfg: dict, premium: bool) -> list:
    panel_ch = cfg.get("panel_channel_id")
    role_id = cfg.get("support_role_id")
    cat_id = cfg.get("category_id")
    msg = cfg.get("welcome_message") or ""
    msg_preview = (msg[:80] + "…") if len(msg) > 80 else msg
    posted = bool(cfg.get("panel_message_id"))

    # Parse premium fields
    cats = []
    try:
        cats = json.loads(cfg["categories_json"]) if cfg.get("categories_json") else []
    except Exception:
        pass
    buttons = []
    try:
        buttons = json.loads(cfg["custom_buttons_json"]) if cfg.get("custom_buttons_json") else []
    except Exception:
        pass
    auto_close = cfg.get("auto_close_hours")
    transcript_ch = cfg.get("transcript_channel_id")
    close_msg = cfg.get("custom_close_message") or ""

    lines = [
        f"{'✅' if panel_ch else '⬜'} **Step 1: Panel channel** — {f'<#{panel_ch}>' if panel_ch else '*not set*'}",
        f"{'✅' if role_id else '⬜'} **Step 2: Support role** — {f'<@&{role_id}>' if role_id else '*not set*'}",
        f"{'✅' if cat_id else '⬜'} **Step 3: Ticket category** — {f'<#{cat_id}>' if cat_id else '*not set*'}",
        f"✅ **Step 4: Welcome message** — {msg_preview or '*default*'}",
        f"-# Panel status: {'posted — <#' + str(panel_ch) + '>' if posted else 'not posted yet'}",
    ]
    if premium:
        lines += [
            "",
            "**💎 Premium options:**",
            f"{'✅' if cats else '⬜'} **Categories** — {len(cats)} configured" if cats else "⬜ **Categories** — none (uses default)",
            f"{'✅' if buttons else '⬜'} **Custom buttons** — {len(buttons)} added" if buttons else "⬜ **Custom buttons** — none",
            f"{'✅' if auto_close else '⬜'} **Auto-close** — {'after ' + str(auto_close) + 'h inactivity' if auto_close else 'off'}",
            f"{'✅' if transcript_ch else '⬜'} **Transcripts** — {f'<#{transcript_ch}>' if transcript_ch else 'off'}",
            f"{'✅' if close_msg else '⬜'} **Close message** — {(close_msg[:60] + '…') if len(close_msg) > 60 else close_msg or '*default*'}",
        ]
    else:
        lines += [
            "",
            "-# 💎 **Premium unlocks:** multiple categories, custom buttons, auto-close, transcripts, custom close message.",
        ]
    return lines


def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"ticketwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


_PAT = r"^ticketwz_([^:]+):(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(
        interaction, invoker_id, "ticket", "manage_guild", "Manage Server", admin_override=True
    )


async def _premium_gate(interaction: discord.Interaction, guild_id: int, clone_id) -> bool:
    if await _is_premium(guild_id, clone_id):
        return True
    from discord_bot.cogs._views_premium import send_premium_pitch
    await interaction.response.defer(ephemeral=True)
    await send_premium_pitch(interaction, guild_id, clone_id)
    return False


def build_wizard_view(guild: discord.Guild, config_row: dict, premium: bool) -> discord.ui.LayoutView:
    guild_id = guild.id
    clone_id = config_row.get("clone_id")
    invoker_id = config_row.get("wizard_invoker_id")

    def enc(field):
        return _encode(field, guild_id, clone_id, invoker_id)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(config_row))

    # Free rows
    ch_row = discord.ui.ActionRow()
    ch_row.add_item(TicketPanelChannelSelect(guild_id, clone_id, invoker_id, guild))

    role_row = discord.ui.ActionRow()
    role_row.add_item(TicketSupportRoleSelect(guild_id, clone_id, invoker_id))

    cat_row = discord.ui.ActionRow()
    cat_row.add_item(TicketCategorySelect(guild_id, clone_id, invoker_id, guild))

    msg_row = discord.ui.ActionRow()
    msg_row.add_item(TicketWelcomeMessageButton(guild_id, clone_id, invoker_id))

    post_row = discord.ui.ActionRow()
    post_row.add_item(TicketPostPanelButton(guild_id, clone_id, invoker_id))
    if not premium:
        post_row.add_item(TicketGoPremiumButton(guild_id, clone_id, invoker_id))

    # Premium rows
    cats_row = discord.ui.ActionRow()
    cats_row.add_item(TicketCategoriesButton(guild_id, clone_id, invoker_id, premium))

    buttons_row = discord.ui.ActionRow()
    buttons_row.add_item(TicketCustomButtonsButton(guild_id, clone_id, invoker_id, premium))

    autoclose_row = discord.ui.ActionRow()
    autoclose_row.add_item(TicketAutoCloseSelect(guild_id, clone_id, invoker_id, config_row, premium))

    transcript_row = discord.ui.ActionRow()
    transcript_row.add_item(TicketTranscriptChannelSelect(guild_id, clone_id, invoker_id, premium))

    closemsg_row = discord.ui.ActionRow()
    closemsg_row.add_item(TicketCloseMessageButton(guild_id, clone_id, invoker_id, premium))

    text = discord.ui.TextDisplay(
        "\n".join(["### 🎫 Ticket Setup", *render_status_lines(config_row, premium)])
    )
    for item in (
        text, discord.ui.Separator(),
        ch_row, role_row, cat_row, msg_row,
        discord.ui.Separator(),
        cats_row, buttons_row, autoclose_row, transcript_row, closemsg_row,
        discord.ui.Separator(),
        post_row,
    ):
        container.add_item(item)

    view.add_item(container)
    return view


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_ticket_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id):
    if not interaction.response.is_done():
        await interaction.response.defer()
    guild = interaction.client.get_guild(guild_id)
    if guild is None:
        return
    cfg = await db.get_ticket_config(guild_id, clone_id=clone_id)
    premium = await _is_premium(guild_id, clone_id)
    view = build_wizard_view(guild, cfg, premium)
    await interaction.edit_original_response(view=view)


# ── Free-tier components ──────────────────────────────────────────────────

class TicketPanelChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect],
                                template=r"^ticketwz_panelch:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, guild=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 1: Panel channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("panelch", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    panel_channel_id=self.item.values[0].id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketSupportRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect],
                               template=r"^ticketwz_role:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="Step 2: Support role",
            min_values=1, max_values=1,
            custom_id=_encode("role", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    support_role_id=self.item.values[0].id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketCategorySelect(discord.ui.DynamicItem[discord.ui.ChannelSelect],
                            template=r"^ticketwz_ticketcat:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, guild=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 3: Ticket category (folder)",
            channel_types=[discord.ChannelType.category],
            min_values=1, max_values=1,
            custom_id=_encode("ticketcat", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    category_id=self.item.values[0].id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketWelcomeMessageModal(discord.ui.Modal, title="Ticket welcome message"):
    def __init__(self, guild_id, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.msg = discord.ui.TextInput(
            label="Welcome message ({member}, {guild} supported)",
            default=current or "",
            style=discord.TextStyle.paragraph,
            max_length=MAX_MESSAGE_LEN, required=False,
        )
        self.add_item(self.msg)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    welcome_message=str(self.msg.value) or None)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketWelcomeMessageButton(discord.ui.DynamicItem[discord.ui.Button],
                                  template=r"^ticketwz_welmsg:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Step 4: Welcome message",
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("welmsg", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        cfg = await db.get_ticket_config(self.guild_id, self.clone_id)
        await interaction.response.send_modal(
            TicketWelcomeMessageModal(self.guild_id, self.clone_id, self.invoker_id, cfg.get("welcome_message"))
        )


class TicketPostPanelButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"^ticketwz_post:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="📋 Post panel", style=discord.ButtonStyle.success,
            custom_id=_encode("post", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await db.get_ticket_config(self.guild_id, clone_id=self.clone_id)
        if not cfg.get("panel_channel_id"):
            await interaction.followup.send("Set a panel channel first (Step 1).", ephemeral=True)
            return
        channel = interaction.client.get_channel(cfg["panel_channel_id"])
        if channel is None:
            await interaction.followup.send("Panel channel not found — pick it again.", ephemeral=True)
            return

        # Build panel buttons — default + any custom premium buttons
        panel_view = discord.ui.View(timeout=None)
        # Same custom_id as TicketPanelView's persistent button in ticket.py,
        # so the existing handler services clicks.
        panel_view.add_item(discord.ui.Button(
            label="Open Ticket", style=discord.ButtonStyle.primary,
            emoji="🎫", custom_id="ticket:open",
        ))
        try:
            custom_btns = json.loads(cfg.get("custom_buttons_json") or "[]")
            for btn in custom_btns[:MAX_CUSTOM_BUTTONS]:
                if btn.get("url"):
                    panel_view.add_item(discord.ui.Button(
                        label=btn.get("label", "Info"),
                        emoji=btn.get("emoji") or None,
                        url=btn["url"],
                        style=discord.ButtonStyle.link,
                    ))
        except Exception:
            pass

        # Build embed with categories if premium
        cats = []
        try:
            cats = json.loads(cfg.get("categories_json") or "[]")
        except Exception:
            pass

        embed = discord.Embed(
            title="🎫 Support Tickets",
            description=(
                "Click **Open Ticket** to create a private support channel.\n\n"
                + ("\n".join(f"{c.get('emoji', '•')} **{c['label']}** — {c.get('description', '')}" for c in cats)
                   if cats else "")
            ),
            color=discord.Color.blurple(),
        )

        try:
            posted = await channel.send(embed=embed, view=panel_view)
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"Couldn't post: {e}", ephemeral=True)
            return

        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    panel_channel_id=channel.id,
                                    panel_message_id=posted.id)
        await _rerender(interaction, self.guild_id, self.clone_id)
        await interaction.followup.send(f"✅ Panel posted in {channel.mention}!", ephemeral=True)


class TicketGoPremiumButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"^ticketwz_goprem:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=f"Go Premium 💎 — ${config.PREMIUM_FEE_USD:g}/month",
            style=discord.ButtonStyle.primary,
            custom_id=_encode("goprem", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid)

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, self.guild_id, self.clone_id)


# ── Premium components ────────────────────────────────────────────────────

class TicketCategoriesModal(discord.ui.Modal, title="💎 Ticket categories (one per line)"):
    def __init__(self, guild_id, clone_id, invoker_id, current_json: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        try:
            rows = json.loads(current_json) if current_json else []
            default = "\n".join(f"{r.get('emoji','🎫')} {r['label']} | {r.get('description','')}" for r in rows)
        except Exception:
            default = ""
        self.cats = discord.ui.TextInput(
            label=f"emoji label | description (max {MAX_CATEGORIES})",
            placeholder="🎫 Support | General help\n💰 Billing | Payment issues\n⚖️ Appeals | Ban appeals",
            default=default, style=discord.TextStyle.paragraph,
            required=False, max_length=600,
        )
        self.add_item(self.cats)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        parsed = []
        for line in self.cats.value.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            label_part = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else ""
            # First token may be emoji
            tokens = label_part.split(None, 1)
            if len(tokens) == 2 and len(tokens[0]) <= 2:
                emoji, label = tokens[0], tokens[1]
            else:
                emoji, label = "🎫", label_part
            if not label:
                continue
            parsed.append({"emoji": emoji, "label": label[:80], "description": description[:100]})
            if len(parsed) >= MAX_CATEGORIES:
                break
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    categories_json=json.dumps(parsed) if parsed else None)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketCategoriesButton(discord.ui.DynamicItem[discord.ui.Button],
                              template=r"^ticketwz_cats:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, premium: bool):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=_lock(f"Ticket categories (max {MAX_CATEGORIES})", premium),
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("cats", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid, False)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if not await _premium_gate(interaction, self.guild_id, self.clone_id):
            return
        cfg = await db.get_ticket_config(self.guild_id, self.clone_id)
        await interaction.response.send_modal(
            TicketCategoriesModal(self.guild_id, self.clone_id, self.invoker_id, cfg.get("categories_json"))
        )


class TicketCustomButtonsModal(discord.ui.Modal, title="💎 Custom panel buttons"):
    def __init__(self, guild_id, clone_id, invoker_id, current_json: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        try:
            rows = json.loads(current_json) if current_json else []
            default = "\n".join(f"{r.get('emoji','')}{' ' if r.get('emoji') else ''}{r['label']} | {r.get('url','')}" for r in rows)
        except Exception:
            default = ""
        self.btns = discord.ui.TextInput(
            label=f"emoji label | URL (max {MAX_CUSTOM_BUTTONS} lines)",
            placeholder="📚 Docs | https://docs.example.com\n🌐 Website | https://example.com",
            default=default, style=discord.TextStyle.paragraph,
            required=False, max_length=400,
        )
        self.add_item(self.btns)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        parsed = []
        for line in self.btns.value.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) != 2:
                await interaction.followup.send(f"Bad line `{line}` — use `emoji label | URL`.", ephemeral=True)
                return
            label_part = parts[0].strip()
            url = parts[1].strip()
            if not url.startswith("http"):
                await interaction.followup.send(f"URL must start with http(s): `{url}`", ephemeral=True)
                return
            tokens = label_part.split(None, 1)
            if len(tokens) == 2 and len(tokens[0]) <= 2:
                emoji, label = tokens[0], tokens[1]
            else:
                emoji, label = None, label_part
            if not label:
                continue
            parsed.append({"emoji": emoji, "label": label[:80], "url": url})
            if len(parsed) >= MAX_CUSTOM_BUTTONS:
                break
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    custom_buttons_json=json.dumps(parsed) if parsed else None)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketCustomButtonsButton(discord.ui.DynamicItem[discord.ui.Button],
                                 template=r"^ticketwz_custbtn:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, premium: bool):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=_lock(f"Custom panel buttons (max {MAX_CUSTOM_BUTTONS})", premium),
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("custbtn", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid, False)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if not await _premium_gate(interaction, self.guild_id, self.clone_id):
            return
        cfg = await db.get_ticket_config(self.guild_id, self.clone_id)
        await interaction.response.send_modal(
            TicketCustomButtonsModal(self.guild_id, self.clone_id, self.invoker_id, cfg.get("custom_buttons_json"))
        )


class TicketAutoCloseSelect(discord.ui.DynamicItem[discord.ui.Select],
                             template=r"^ticketwz_autoclose:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, cfg: dict, premium: bool):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = cfg.get("auto_close_hours")
        options = [discord.SelectOption(label="Off", value="0", default=(not current))]
        for h in [1, 2, 6, 12, 24, 48, 72]:
            options.append(discord.SelectOption(
                label=f"After {h}h inactivity", value=str(h), default=(h == current)
            ))
        super().__init__(discord.ui.Select(
            placeholder=_lock("💎 Auto-close inactive tickets", premium),
            options=options,
            custom_id=_encode("autoclose", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid, {}, False)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if not await _premium_gate(interaction, self.guild_id, self.clone_id):
            return
        val = int(self.item.values[0])
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    auto_close_hours=val if val else None)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketTranscriptChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect],
                                     template=r"^ticketwz_transcript:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, premium: bool):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder=_lock("💎 Transcript log channel", premium),
            channel_types=[discord.ChannelType.text],
            min_values=0, max_values=1,
            custom_id=_encode("transcript", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid, False)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if not await _premium_gate(interaction, self.guild_id, self.clone_id):
            return
        ch_id = self.item.values[0].id if self.item.values else None
        await db.set_ticket_config(self.guild_id, self.clone_id, transcript_channel_id=ch_id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketCloseMessageModal(discord.ui.Modal, title="💎 Custom close message"):
    def __init__(self, guild_id, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.msg = discord.ui.TextInput(
            label="Message shown when a ticket is closed",
            placeholder="Thanks for contacting support! Your ticket has been closed.",
            default=current or "",
            style=discord.TextStyle.paragraph,
            max_length=300, required=False,
        )
        self.add_item(self.msg)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await db.set_ticket_config(self.guild_id, self.clone_id,
                                    custom_close_message=str(self.msg.value) or None)
        await _rerender(interaction, self.guild_id, self.clone_id)


class TicketCloseMessageButton(discord.ui.DynamicItem[discord.ui.Button],
                                template=r"^ticketwz_closemsg:(\d+):(-|\d+):(-|\d+)$"):
    def __init__(self, guild_id, clone_id, invoker_id, premium: bool):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=_lock("Custom close message", premium),
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("closemsg", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        gid, cid, iid = _decode(match)
        return cls(gid, cid, iid, False)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if not await _premium_gate(interaction, self.guild_id, self.clone_id):
            return
        cfg = await db.get_ticket_config(self.guild_id, self.clone_id)
        await interaction.response.send_modal(
            TicketCloseMessageModal(self.guild_id, self.clone_id, self.invoker_id, cfg.get("custom_close_message"))
        )


DYNAMIC_ITEMS = (
    TicketPanelChannelSelect, TicketSupportRoleSelect, TicketCategorySelect,
    TicketWelcomeMessageButton, TicketPostPanelButton, TicketGoPremiumButton,
    TicketCategoriesButton, TicketCustomButtonsButton,
    TicketAutoCloseSelect, TicketTranscriptChannelSelect, TicketCloseMessageButton,
)
