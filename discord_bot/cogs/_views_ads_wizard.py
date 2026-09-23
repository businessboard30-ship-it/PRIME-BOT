# path: discord_bot/cogs/_views_ads_wizard.py

"""
Owner ad manager for /ad manage — the wizard for the PAID network ads (the
ones from /ad submit and the join DM's "Advertise with us" button, stored in
ad_submissions). Not to be confused with the per-server /watchad sponsor in
_views_economy_wizard.py.

No ad ids are ever typed: pick an ad from a menu (pending ones first), then
act on it with buttons — Approve / Reject (asks for a reason) while pending,
Deactivate while live, Reactivate while deactivated, Edit any time.

Same DynamicItem / restart-proof pattern as the other wizards: the selected
ad's id rides in each button's custom_id, and every callback re-checks the
owner gate (DISCORD_CLONE_ADMIN_IDS — the same gate /ad approve uses), so a
stale or forwarded message can't be used by anyone else.
"""

import logging
import re

import discord

from config import DISCORD_CLONE_ADMIN_IDS
from modules.ads_marketplace import (
    get_ad, list_ads_for_manager, count_ads_by_status,
    approve_ad, reject_ad, deactivate_ad, reactivate_ad, update_ad_fields, set_ad_image,
)
from discord_bot.ad_images import upload_ad_image

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {"pending": "⏳", "approved": "🟢", "deactivated": "⏸️", "rejected": "❌"}
_STATUS_LABEL = {"pending": "Pending", "approved": "Live", "deactivated": "Deactivated", "rejected": "Rejected"}


def _is_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _clip(text, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _counts_line(counts: dict) -> str:
    return "  ·  ".join(
        f"{_STATUS_EMOJI[k]} {counts.get(k, 0)} {_STATUS_LABEL[k].lower()}"
        for k in ("pending", "approved", "deactivated")
    )


def _ad_lines(ad: dict) -> list:
    emoji = _STATUS_EMOJI.get(ad["status"], "•")
    lines = [
        f"**#{ad['id']} — {_clip(ad['company_name'], 100)}**  {emoji} {_STATUS_LABEL.get(ad['status'], ad['status'])}",
        f"**{_clip(ad['ad_title'], 200)}**",
        _clip(ad["ad_description"], 600),
        f"🔗 {_clip(ad['target_url'], 200)}  ·  💵 ${ad['budget_usd']}" + ("  ·  🖼️ image" if ad.get("image_message_id") else ""),
    ]
    if ad.get("rejection_reason"):
        lines.append(f"Rejection reason: {_clip(ad['rejection_reason'], 200)}")
    return lines


def build_manager_view(ads: list, counts: dict, selected: dict = None, note: str = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.gold())

    head = ["### 📢 Manage ads", _counts_line(counts)]
    if note:
        head.append(note)
    container.add_item(discord.ui.TextDisplay("\n".join(head)))
    container.add_item(discord.ui.Separator())

    if selected:
        container.add_item(discord.ui.TextDisplay("\n".join(_ad_lines(selected))))
        container.add_item(discord.ui.Separator())

    if ads or selected:
        listed = list(ads)
        if selected and all(a["id"] != selected["id"] for a in listed):
            listed = [selected] + listed[:24]
        pick_row = discord.ui.ActionRow()
        pick_row.add_item(AdPickSelect(listed, selected["id"] if selected else None))
        container.add_item(pick_row)
    else:
        container.add_item(discord.ui.TextDisplay("No ads have been submitted yet."))

    if selected:
        status = selected["status"]
        acts = []
        if status == "pending":
            acts += ["approve", "reject"]
        elif status == "approved":
            acts.append("deactivate")
        elif status == "deactivated":
            acts.append("reactivate")
        acts.append("edit")
        action_row = discord.ui.ActionRow()
        for act in acts:
            action_row.add_item(AdActionButton(act, selected["id"]))
        container.add_item(action_row)

    view.add_item(container)
    return view


async def _load(selected_id: int = None):
    ads = await list_ads_for_manager()
    counts = await count_ads_by_status()
    selected = await get_ad(selected_id) if selected_id else None
    if selected and selected["status"] == "rejected":
        selected = None  # rejected ads vanish from the manager
    return ads, counts, selected


async def render(interaction: discord.Interaction, selected_id: int = None, note: str = None):
    """Rebuild the manager in place. Uses edit_message for a fresh component
    click, or edit_original_response when the caller already deferred (modals)."""
    ads, counts, selected = await _load(selected_id)
    view = build_manager_view(ads, counts, selected, note)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


async def _deny(interaction: discord.Interaction):
    msg = "You're not authorized to manage ads."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def notify_rejected(client: discord.Client, ad: dict, reason: str) -> bool:
    """DM the submitter that their ad was rejected, with the reason. Returns
    False when the DM couldn't be delivered (DMs closed, user not found) —
    callers surface that to the owner; /ad status still shows the reason."""
    try:
        user = client.get_user(ad["user_id"]) or await client.fetch_user(ad["user_id"])
        embed = discord.Embed(
            title=f"❌ Your ad #{ad['id']} was not approved",
            description=(
                f"**{_clip(ad['company_name'], 100)} — {_clip(ad['ad_title'], 200)}**\n\n"
                f"**Reason:** {reason or 'No reason given.'}\n\n"
                "You can fix the issue and submit a new ad with `/ad submit`. "
                f"You can also check this any time with `/ad status ad_id:{ad['id']}`."
            ),
            color=discord.Color.red(),
        )
        await user.send(embed=embed)
        return True
    except (discord.HTTPException, discord.NotFound) as e:
        logger.info(f"[ads] couldn't DM rejection for ad {ad.get('id')}: {e}")
        return False


class AdPickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"adwz_pick"):
    def __init__(self, ads: list = None, selected_id: int = None):
        options = [
            discord.SelectOption(
                label=_clip(f"#{a['id']} {a['company_name']} — {a['ad_title']}", 100),
                value=str(a["id"]),
                description=f"{_STATUS_EMOJI.get(a['status'], '')} {_STATUS_LABEL.get(a['status'], a['status'])}",
                default=(a["id"] == selected_id),
            )
            for a in (ads or [])[:25]
        ] or [discord.SelectOption(label="No ads", value="0")]
        super().__init__(discord.ui.Select(
            placeholder="Pick an ad to manage…", options=options, custom_id="adwz_pick",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        ad_id = int(self.item.values[0])
        await render(interaction, ad_id or None)


_ACTION_STYLE = {
    "approve": ("✅ Approve", discord.ButtonStyle.success),
    "reject": ("✖️ Reject", discord.ButtonStyle.danger),
    "deactivate": ("⏸️ Deactivate", discord.ButtonStyle.danger),
    "reactivate": ("▶️ Reactivate", discord.ButtonStyle.success),
    "edit": ("✏️ Edit", discord.ButtonStyle.secondary),
}


class AdActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"adwz_(?P<act>approve|reject|deactivate|reactivate|edit):(?P<ad_id>\d+)",
):
    def __init__(self, act: str, ad_id: int):
        self.act = act
        self.ad_id = ad_id
        label, style = _ACTION_STYLE[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"adwz_{act}:{ad_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match.group("act"), int(match.group("ad_id")))

    async def callback(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        ad = await get_ad(self.ad_id)
        if not ad:
            await render(interaction, None, "❌ That ad no longer exists.")
            return
        if self.act == "edit":
            await interaction.response.send_modal(AdEditModal(ad))
            return
        if self.act == "reject":
            await interaction.response.send_modal(AdRejectModal(ad))
            return
        action, done_note = {
            "approve": (approve_ad, "✅ Approved — it's live."),
            "deactivate": (deactivate_ad, "⏸️ Deactivated — it's off every surface."),
            "reactivate": (reactivate_ad, "▶️ Live again."),
        }[self.act]
        ok = await action(self.ad_id)
        await render(interaction, self.ad_id, done_note if ok else "❌ That ad changed while you were looking at it — here's its current state.")


class AdRejectModal(discord.ui.Modal, title="Reject ad"):
    def __init__(self, ad: dict):
        super().__init__()
        self.ad_id = ad["id"]
        self.reason = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=200)
        self.add_item(discord.ui.Label(
            text="Reason", description="Sent to the submitter in a DM", component=self.reason,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer()
        reason = str(self.reason.value).strip()[:200]
        ad = await get_ad(self.ad_id)
        ok = await reject_ad(self.ad_id, reason)
        if not ok:
            await render(interaction, self.ad_id, "❌ Couldn't reject — it was already reviewed.")
            return
        sent = await notify_rejected(interaction.client, ad, reason) if ad else False
        await render(
            interaction, None,
            f"❌ Ad #{self.ad_id} rejected and removed from this list. "
            + ("Submitter notified by DM." if sent else "Couldn't DM the submitter (DMs closed) — they can still see the reason in /ad status."),
        )


class AdEditModal(discord.ui.Modal, title="Edit ad"):
    def __init__(self, ad: dict):
        super().__init__()
        self.ad_id = ad["id"]
        self.company = discord.ui.TextInput(default=_clip(ad["company_name"], 100), max_length=100)
        self.headline = discord.ui.TextInput(default=_clip(ad["ad_title"], 200), max_length=200)
        self.body = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, default=_clip(ad["ad_description"], 1000), max_length=1000)
        self.link = discord.ui.TextInput(default=_clip(ad["target_url"], 300), max_length=300)
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(discord.ui.Label(text="Company / brand", component=self.company))
        self.add_item(discord.ui.Label(text="Headline", component=self.headline))
        self.add_item(discord.ui.Label(text="Body text", component=self.body))
        self.add_item(discord.ui.Label(text="Link (https://… or N/A)", component=self.link))
        self.add_item(discord.ui.Label(
            text="Image (upload or replace)",
            description="png/jpeg/gif/webp, max 8MB — leave empty to keep the current one",
            component=self.upload,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        link = str(self.link.value).strip()
        if link.upper() != "N/A" and not re.match(r"^https?://\S+$", link):
            await interaction.followup.send("The link needs to start with http:// or https:// (or be N/A).", ephemeral=True)
            return
        ok = await update_ad_fields(
            self.ad_id,
            company_name=str(self.company.value).strip(),
            ad_title=str(self.headline.value).strip(),
            ad_description=str(self.body.value).strip(),
            target_url=link,
        )
        if not ok:
            await render(interaction, self.ad_id, "❌ Couldn't save that edit.")
            return
        note = "✏️ Saved."
        files = list(self.upload.values or [])
        if files:
            channel_id, message_id, reason = await upload_ad_image(interaction.client, files[0], interaction.user, self.ad_id)
            if reason:
                note = f"✏️ Text saved, but the image wasn't changed — {reason}."
            elif await set_ad_image(self.ad_id, None, channel_id, message_id):
                note = "✏️ Saved (text + image)."
            else:
                note = "✏️ Text saved, but the image couldn't be attached. Try again."
        await render(interaction, self.ad_id, note)


DYNAMIC_ITEMS = (AdPickSelect, AdActionButton)
