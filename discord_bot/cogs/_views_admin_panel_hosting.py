"""
Owner panel: the Hosting wizard (System > Hosting wizard).

One screen that shows where uploads and cat pictures are stored right now and lets the owner change it with a channel
picker (no need to open the panel inside the channel):

  1. Pick a channel.
  2. Choose what it is for: cat images, welcome backgrounds, or both. Each choice asks for a second press (Confirm).
  3. Test it.

Changing a channel NEVER deletes or moves anything. Every saved cat picture remembers its own channel and message, so cats
pulled earlier keep loading from where they were saved and only NEW cats go to the new channel. The screen says so, and
lists how many pictures live in which channel.

Same access rule as /admin hostingchannel: bot owners only (DISCORD_OWNER_BROADCAST_IDS), re-checked on every click.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import discord

from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import PanelView, _btn, audit

logger = logging.getLogger(__name__)

SYSTEM = "system"
TARGETS = {
    "cats": "cat images",
    "welcome": "welcome backgrounds",
    "both": "cat images and welcome backgrounds",
}


def _mention(channel_id: int) -> str:
    return f"<#{channel_id}>" if channel_id else "not set"


def describe_state(snap: dict) -> List[str]:
    """The 'where is everything now' lines (pure, no Discord calls)."""
    host, default, cat = snap.get("host_id", 0), snap.get("default_id", 0), snap.get("cat_id", 0)
    lines = []
    if host:
        lines.append(f"🖼️ **Welcome backgrounds and ads:** {_mention(host)} (set by you)")
    else:
        lines.append(f"🖼️ **Welcome backgrounds and ads:** {_mention(default)} (the default from `IMAGE_HOST_CHANNEL_ID`)")
    if cat:
        lines.append(f"🐱 **New cat images and cat logs:** {_mention(cat)} (set by you)")
    else:
        lines.append(f"🐱 **New cat images and cat logs:** follow the welcome channel ({_mention(host or default)})")
    saved = snap.get("saved") or []
    if saved:
        lines.append("📦 **Cat pictures already saved:**")
        for cid, pics, adopted in saved:
            lines.append(f"-# {_mention(cid)}: {pics:,} picture{'s' if pics != 1 else ''}, {adopted:,} adopted by players")
    else:
        lines.append("📦 No cat pictures are saved yet.")
    return lines


def change_warning() -> str:
    return ("✅ **Changing a channel never deletes or moves anything.** Cats already pulled keep loading from the channel where "
            "they were saved; only new cats go to the new channel. ⚠️ Don't delete the old channel or its messages, and keep every "
            "bot (main and clones) able to read it. A picture whose message is gone is dropped from the pool for good.")


async def load_snapshot() -> dict:
    """What the wizard shows. Each part fails on its own so one problem never hides the rest."""
    import config
    from modules import image_host as ih
    snap = {"host_id": 0, "cat_id": 0, "default_id": int(getattr(config, "IMAGE_HOST_CHANNEL_ID", 0) or 0), "saved": []}
    try:
        snap["host_id"] = await ih.setting_id(ih.HOST_SETTING)
        snap["cat_id"] = await ih.setting_id(ih.CAT_SETTING)
    except Exception:
        logger.warning("[hosting-wizard] couldn't read the settings", exc_info=True)
    try:
        snap["saved"] = await ih.saved_cat_pictures()
    except Exception:
        logger.warning("[hosting-wizard] couldn't count the saved cat pictures", exc_info=True)
    return snap


class HostingWizardView(PanelView):
    title = "🧙 Hosting wizard"

    def __init__(self, cog, owner_id, snapshot: dict, picked: Optional[int] = None, picked_ok: bool = False,
                 note: Optional[str] = None, confirm: Optional[str] = None):
        self.snapshot, self.picked, self.picked_ok, self.note, self._confirm = snapshot, picked, picked_ok, note, confirm
        super().__init__(cog, owner_id, SYSTEM)

    @classmethod
    async def create(cls, cog, owner_id, **kw) -> "HostingWizardView":
        return cls(cog, owner_id, await load_snapshot(), **kw)

    # ── screen ───────────────────────────────────────────────────────────
    def body(self) -> List[str]:
        lines = describe_state(self.snapshot)
        lines.append("")
        if self.note:
            lines += [self.note, ""]
        if self.picked and self.picked_ok:
            if self._confirm:
                lines.append(f"⚠️ **Press Confirm to use {_mention(self.picked)} for {TARGETS[self._confirm]}.**")
                lines.append(change_warning())
            else:
                lines.append(f"Picked {_mention(self.picked)}. What should it be used for?")
        else:
            lines.append("**Step 1:** pick a text channel below. Then choose what it is for, and test it.")
        return lines

    def controls(self):
        P, S, G = discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success
        ready = bool(self.picked and self.picked_ok)
        picker = discord.ui.ChannelSelect(placeholder="Pick the hosting channel…", channel_types=[discord.ChannelType.text],
                                          min_values=1, max_values=1)
        picker.callback = self._pick
        items: list = [picker]
        for key, label, emoji in (("cats", "Use for cat images", "🐱"), ("welcome", "Use for welcome backgrounds", "🖼️"),
                                  ("both", "Use for both", "🔁")):
            on = self._confirm == key
            items.append(_btn("Confirm" if on else label, G if on else P, self._use(key), "✅" if on else emoji,
                              disabled=not ready or (self._confirm is not None and not on)))
        items.append(_btn("Cats follow the welcome channel", S, self._reset_cats, "🔗", disabled=not self.snapshot.get("cat_id")))
        items.append(_btn("Test hosting", S, self._test, "🧪"))
        items.append(_btn("Back", S, self._back, "⬅️"))
        return items

    async def _redraw(self, i: discord.Interaction, **kw) -> None:
        fresh = await load_snapshot()
        view = HostingWizardView(self.cog, self.owner_id, fresh, **kw)
        if i.response.is_done():
            await i.followup.edit_message(i.message.id, view=view)
        else:
            await i.response.edit_message(view=view)

    # ── actions ──────────────────────────────────────────────────────────
    async def _pick(self, i: discord.Interaction):
        from modules import image_host as ih
        select = next((c for c in self.walk_children() if isinstance(c, discord.ui.ChannelSelect)), None)
        raw = (i.data or {}).get("values") or []
        channel_id = int(raw[0]) if raw else (select.values[0].id if select is not None and select.values else 0)
        if not channel_id:
            await i.response.defer()
            return
        await i.response.defer()
        channel, why = await ih.open_channel(i.client, channel_id)
        if channel is None:
            await self._redraw(i, picked=channel_id, picked_ok=False, note=f"❌ {_mention(channel_id)}: {why}.")
            return
        problem = ih.permission_problem(channel, i.client)
        if problem:
            await self._redraw(i, picked=channel_id, picked_ok=False, note=f"❌ {_mention(channel_id)}: {problem}.")
            return
        await self._redraw(i, picked=channel_id, picked_ok=True,
                           note=f"✅ This bot can store pictures in {_mention(channel_id)}. "
                                "-# Clone bots are checked when they first save a picture there; they must be in that server too.")

    def _use(self, key: str):
        async def cb(i: discord.Interaction):
            if not (self.picked and self.picked_ok):
                await i.response.defer()
                return
            if self._confirm != key:
                await i.response.defer()
                await self._redraw(i, picked=self.picked, picked_ok=True, confirm=key)
                return
            await i.response.defer()
            await self._apply(i, key)
        return cb

    async def _apply(self, i: discord.Interaction, key: str) -> None:
        from modules import image_host as ih
        channel_id = int(self.picked)
        audit(i, "system.hostingwizard", target=key, channel=channel_id)
        try:
            if key in ("welcome", "both"):
                await ih.set_setting(ih.HOST_SETTING, channel_id)
            if key in ("cats", "both"):
                await ih.set_setting(ih.CAT_SETTING, channel_id)
        except Exception:
            logger.exception("[hosting-wizard] couldn't save the setting")
            await self._redraw(i, note="❌ Couldn't save that. Nothing was changed. Check the bot logs.")
            return
        extra = ""
        if key in ("welcome", "both"):
            extra = " Saved welcome backgrounds are being moved there now (clones move theirs on their next 12-hourly run)."
            asyncio.create_task(ih.auto_move(i.client))
        await self._redraw(i, note=f"✅ Done! {_mention(channel_id)} is now used for {TARGETS[key]}.{extra} Cat pictures saved earlier stay where they are and keep working.")

    async def _reset_cats(self, i: discord.Interaction):
        from modules import image_host as ih
        audit(i, "system.hostingwizard.catsreset")
        await i.response.defer()
        try:
            await ih.set_setting(ih.CAT_SETTING, None)
        except Exception:
            logger.exception("[hosting-wizard] couldn't clear the cat channel")
            await self._redraw(i, note="❌ Couldn't save that. Nothing was changed.")
            return
        await self._redraw(i, note="🔗 New cat images now follow the welcome channel again. Cats saved earlier stay where they are.")

    async def _test(self, i: discord.Interaction):
        """Same diagnostic as System > Test image hosting, plus a note on which channel new cat images use."""
        from modules import image_host as ih
        audit(i, "system.hostingwizard.test")
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            checks, card = await ih.diagnose(i.client)
            text = ih.format_report(checks, ih.bot_label(i.client))
            cat = await ih.resolve_cat_channel(i.client)
            text += ("\n🐱 New cat images go to " + (_mention(cat.id) if cat else "**nowhere right now**: this bot can't reach any hosting channel") + ".")
        except Exception:
            logger.exception("[hosting-wizard] test crashed")
            await i.followup.send("❌ The hosting test itself crashed. Check the bot logs.", ephemeral=True)
            return
        files = []
        if card:
            import io
            files.append(discord.File(io.BytesIO(card), filename="test-welcome-card.png"))
        await i.followup.send(text, files=files, ephemeral=True)

    async def _back(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_system import SystemView
        await self.go(i, SystemView(self.cog, self.owner_id))


def allowed(owner_id: int) -> bool:
    return owner_id in main.DISCORD_OWNER_BROADCAST_IDS
