# path: discord_bot/cogs/_views_admin_panel_scamshield.py

"""
Owner panel: Scam Shield. Shows whether it's on, the rules (words / domains / known scam images),
the latest catches, and lets the owner add or remove rules and flip the global switch.

Everything is read from the database only when this screen is opened or refreshed, never per message.
Rule lists are shown in ONE code block (no inline code, no <t:> timestamps), see the garbled-list
lesson from the Pending payments screen.
"""

from __future__ import annotations

import logging
import re
from typing import List

import discord

from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import TEXT_BUDGET, _denied, _fit
from modules import scam_shield as ss
from modules import scam_vision as sv

logger = logging.getLogger(__name__)

SECTION = "scamshield"
NOTHING_CHANGED = "Couldn't save that (database problem). Nothing was changed."
_MSG_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")


def _age(dt) -> str:
    from datetime import datetime, timezone
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    return f"{secs // 86400}d ago" if secs >= 86400 else f"{secs // 3600}h ago" if secs >= 3600 else f"{secs // 60}m ago"


class AddRuleModal(discord.ui.Modal, title="Add scam word or domain"):
    def __init__(self, view: "ScamShieldView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.panel = view
        self.text = discord.ui.TextInput(label="Word, phrase or domain", max_length=100,
                                         placeholder="e.g. free nitro gift  or  fatowin.com")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        if SECTION not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        kind, pattern = ss.classify(self.text.value)
        if len(pattern) < 4:
            self.panel.notice = "⚠️ That's too short. A tiny word would delete normal messages. Use at least 4 characters."
        else:
            try:
                rid = await ss.add_rule(kind, pattern, interaction.user.id)
            except Exception:
                logger.exception("[admin-panel] couldn't add scam rule")
                self.panel.notice = NOTHING_CHANGED
            else:
                if rid is None:
                    self.panel.notice = "Nothing added: it already exists (or the list is full)."
                else:
                    audit(interaction, "scamshield.add", kind=kind, pattern=pattern)
                    self.panel.notice = f"✅ Added {kind} rule **#{rid}**. Live in every server within seconds."
        await self.panel.load()
        await interaction.response.edit_message(view=self.panel)


class AddImageModal(discord.ui.Modal, title="Add a scam image"):
    def __init__(self, view: "ScamShieldView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.panel = view
        self.link = discord.ui.TextInput(label="Link to a message with the scam image", max_length=200,
                                         placeholder="Right-click the message -> Copy Message Link")
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        if SECTION not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        await interaction.response.defer()
        m = _MSG_LINK.search(self.link.value or "")
        notice = "⚠️ That doesn't look like a message link. Right-click the message -> Copy Message Link."
        if m:
            notice = await self._add_from_message(interaction, int(m.group(2)), int(m.group(3)))
        self.panel.notice = notice
        await self.panel.load()
        await interaction.edit_original_response(view=self.panel)

    async def _add_from_message(self, interaction, channel_id: int, message_id: int) -> str:
        try:
            ch = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
            msg = await ch.fetch_message(message_id)
        except discord.HTTPException:
            return "⚠️ I can't see that message. The bot must be in that server and able to read the channel."
        added, bad = 0, 0
        for att in msg.attachments[:4]:
            if att.size > 8_000_000 or not ((att.content_type or "").startswith("image/")
                                            or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))):
                continue
            try:
                h = ss.dhash(await att.read())
            except discord.HTTPException:
                h = None
            if h is None:
                bad += 1
                continue
            rid = await ss.add_rule("image", format(h, "016x"), interaction.user.id, "added from message link")
            audit(interaction, "scamshield.add", kind="image", pattern=format(h, "016x"))
            added += 1 if rid else 0
        if added:
            return f"✅ Added {added} scam image rule(s). Live in every server within seconds."
        return "Nothing added: no new image found on that message (or it was already a rule)." if not bad else \
            "⚠️ I couldn't read that image."


class RemoveRuleModal(discord.ui.Modal, title="Remove a rule"):
    def __init__(self, view: "ScamShieldView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.panel = view
        self.rid = discord.ui.TextInput(label="Rule number (the # in the list)", max_length=6)
        self.add_item(self.rid)

    async def on_submit(self, interaction: discord.Interaction):
        if SECTION not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        raw = (self.rid.value or "").strip().lstrip("#")
        if not raw.isdigit():
            self.panel.notice = "⚠️ Type just the rule number, like 3."
        else:
            try:
                ok = await ss.remove_rule(int(raw))
            except Exception:
                logger.exception("[admin-panel] couldn't remove scam rule")
                self.panel.notice = NOTHING_CHANGED
            else:
                if ok:
                    audit(interaction, "scamshield.remove", id=int(raw))
                self.panel.notice = f"🗑️ Removed rule #{raw}." if ok else f"No rule #{raw}."
        await self.panel.load()
        await interaction.response.edit_message(view=self.panel)


class ScamShieldView(PanelView):
    title = "🛡️ Scam Shield"

    def __init__(self, cog, owner_id, section=SECTION):
        self.rules: List[dict] = []
        self.hits: List[dict] = []
        self.hit_total = 0
        self.enabled = True
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            await ss.load(force=True)
            self.enabled = ss.is_enabled()
            self.rules = await ss.list_rules()
            self.hits = await ss.recent_hits(8)
            self.hit_total = await ss.hit_total()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load Scam Shield")
            self.error = True
        self._build()

    def body(self) -> List[str]:
        if self.error:
            return ["Couldn't load Scam Shield right now (database problem). Try Refresh."]
        n = {k: sum(1 for r in self.rules if r["kind"] == k) for k in ss.KINDS}
        lines = [
            ("🟢 **ON** in every server the bot is in. Caught messages are deleted and flagged."
             if self.enabled else "🔴 **OFF**. Nothing is being checked."),
            f"Rules: {n['word']} word(s), {n['domain']} domain(s), {n['image']} known scam image(s). "
            f"Caught so far: **{self.hit_total}**.",
            "-# Staff (Manage Messages / Manage Server / Admin) are never checked. Rules refresh by themselves.",
            self._vision_line(),
            self._evidence_line(),
        ]
        if self.notice:
            lines.insert(0, self.notice)
        rule_lines = [f"#{r['id']} {r['kind']:<6} {r['pattern'] if r['kind'] != 'image' else 'image ' + r['pattern'][:8]}"
                      + (f"  ({r['note']})" if r.get("note") else "") for r in self.rules]
        shown = _fit(rule_lines, TEXT_BUDGET // 2)
        more = shown.pop() if shown and shown[-1].startswith("-# …and") else None
        lines.append("**Rules**\n```\n" + ("\n".join(shown) or "none yet") + "\n```" + (f"\n{more}" if more else ""))
        hit_lines = [f"{_age(h['created_at'])} | server {h['guild_id']} | user {h['user_id']} | {h['kind']}"
                     + ("" if h["deleted"] else " | NOT DELETED") for h in self.hits]
        lines.append("**Latest catches**\n```\n" + ("\n".join(hit_lines) or "none yet") + "\n```")
        return lines

    def _vision_line(self) -> str:
        if not sv.api_key():
            return "🤖 **AI image scan:** unavailable (set GEMINI_API_KEY). Only the known-image rules run."
        if not ss.vision_enabled():
            return "🤖 **AI image scan:** 🔴 OFF. Only the known-image rules run."
        return f"🤖 **AI image scan:** 🟢 ON ({sv.model()}). Catches new scam pictures the rules don't know yet."

    def _evidence_line(self) -> str:
        cid = ss.evidence_channel_id()
        if cid:
            return f"📁 **Evidence channel:** <#{cid}> (caught scam text + images are copied here)."
        return "📁 **Evidence channel:** not set, using the image-hosting channel if there is one."

    def controls(self):
        S, P, D = discord.ButtonStyle.secondary, discord.ButtonStyle.primary, discord.ButtonStyle.danger
        return [
            _btn("Turn OFF" if self.enabled else "Turn ON", D if self.enabled else discord.ButtonStyle.success,
                 self._toggle, "⏸️" if self.enabled else "▶️"),
            _btn("Add word / domain", P, self._add, "➕"),
            _btn("Add image", P, self._add_image, "🖼️"),
            _btn("Remove rule", S, self._remove, "🗑️", disabled=not self.rules),
            _btn("AI scan OFF" if ss.vision_enabled() else "AI scan ON", S, self._toggle_vision, "🤖",
                 disabled=not sv.api_key()),
            _btn("Evidence: this channel", S, self._evidence_here, "📁"),
            _btn("Evidence: reset", S, self._evidence_reset, "↩️", disabled=not ss.evidence_channel_id()),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _toggle(self, i):
        try:
            await ss.set_enabled(not self.enabled)
        except Exception:
            logger.exception("[admin-panel] couldn't switch Scam Shield")
            self.notice = NOTHING_CHANGED
        else:
            audit(i, "scamshield.toggle", enabled=not self.enabled)
            self.notice = "▶️ Scam Shield is ON." if not self.enabled else "⏸️ Scam Shield is OFF."
        await self.load()
        await i.response.edit_message(view=self)

    async def _toggle_vision(self, i):
        on = not ss.vision_enabled()
        try:
            await ss.set_vision(on)
        except Exception:
            logger.exception("[admin-panel] couldn't switch the AI image scan")
            self.notice = NOTHING_CHANGED
        else:
            audit(i, "scamshield.vision", enabled=on)
            self.notice = "🤖 AI image scan is ON." if on else "🤖 AI image scan is OFF."
        await self.load()
        await i.response.edit_message(view=self)

    async def _evidence_here(self, i):
        ch = i.channel
        me = i.guild.me if i.guild else None
        if not isinstance(ch, discord.TextChannel) or me is None:
            self.notice = "⚠️ Open this panel in a server text channel to use it as the evidence channel."
        else:
            p = ch.permissions_for(me)
            if not (p.send_messages and p.attach_files and p.read_message_history):
                self.notice = "⚠️ I need Send Messages, Attach Files and Read Message History in this channel."
            else:
                try:
                    await ss.set_evidence_channel(ch.id)
                except Exception:
                    logger.exception("[admin-panel] couldn't save the evidence channel")
                    self.notice = NOTHING_CHANGED
                else:
                    audit(i, "scamshield.evidence", channel=ch.id)
                    self.notice = f"✅ Caught scams will be copied to {ch.mention} from now on."
        await self.load()
        await i.response.edit_message(view=self)

    async def _evidence_reset(self, i):
        try:
            await ss.set_evidence_channel(None)
        except Exception:
            logger.exception("[admin-panel] couldn't clear the evidence channel")
            self.notice = NOTHING_CHANGED
        else:
            audit(i, "scamshield.evidence", channel=None)
            self.notice = "↩️ Evidence channel cleared. Using the image-hosting channel again."
        await self.load()
        await i.response.edit_message(view=self)

    async def _add(self, i):
        await i.response.send_modal(AddRuleModal(self))

    async def _add_image(self, i):
        await i.response.send_modal(AddImageModal(self))

    async def _remove(self, i):
        await i.response.send_modal(RemoveRuleModal(self))

    async def _refresh(self, i):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i):
        from discord_bot.cogs._views_admin_panel_servers import ServersHubView  # lazy: avoids import cycle
        await self.go(i, ServersHubView(self.cog, self.owner_id))
