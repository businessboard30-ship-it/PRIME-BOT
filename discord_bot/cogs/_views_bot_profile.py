"""Bot profile wizard, opened from the join DM's "More features" screen. Premium: anyone can set things up, the
Premium pitch (pay buttons) appears when they tap Save, and their draft waits 30 minutes so one tap applies it
after paying. Reset stays free, and so does a clone owner's global profile (their own bot).

Same building blocks as the welcome-card wizard (_views_card_customize.py / _views_join_dm.py):
  * DynamicItem buttons whose custom_id carries guild_id + clone_id, so they survive restarts and never time out
  * the same Manage Server gate (check_wizard_access, which also works when clicked from a DM)
  * modals with a FileUpload *or* a pasted image link, fetched/validated by welcome._fetch_custom_bg_bytes

What it edits:
  * THIS SERVER'S profile of the bot (nickname, avatar, banner, about me) - PATCH /guilds/{id}/members/@me.
    Works for the main bot and for clones, needs nothing but Manage Server, and changes nothing outside the server.
  * For a clone's owner only: the bot's GLOBAL username, avatar and banner (it is their own bot), through the
    clone's own gateway client - the token is never touched here.
"""

import asyncio
import base64
import io
import logging
import re
import time

import aiohttp
import discord

import config
from database import db
from modules import dthings_vote
from discord_bot.cogs._views_shared import check_wizard_access
from modules import ai_name

logger = logging.getLogger(__name__)

AVATAR_SIZE = 512            # square, centre-cropped
BANNER_SIZE = (1020, 360)    # 17:6, what Discord shows on a profile
NICK_MAX = 32
BIO_MAX = 190                # Discord's limit for a profile "about me"


def _cid(action: str, guild_id: int, clone_id) -> str:
    return f"botprof_{action}:{guild_id}:{'-' if clone_id is None else clone_id}"


def _pat(action: str) -> str:
    return rf"^botprof_{action}:(\d+):(-|\d+)$"


def _ids(match: "re.Match"):
    part = match.group(2)
    return int(match.group(1)), (None if part == "-" else int(part))


async def _check_access(interaction: discord.Interaction, guild_id: int) -> bool:
    return await check_wizard_access(interaction, None, "serversetup", "manage_guild", "Manage Server", guild_id=guild_id)


async def is_clone_owner(clone_id, user_id: int) -> bool:
    if clone_id is None:
        return False
    try:
        row = await db.get_discord_clone(clone_id)
    except Exception as e:
        logger.warning(f"[bot-profile] clone lookup failed for {clone_id}: {e}")
        return False
    return bool(row) and int(row.get("owner_id") or 0) == int(user_id)


# ── images ────────────────────────────────────────────────────────────────

def _prepare_image(data: bytes, kind: str):
    """Validate + re-encode as PNG at the size Discord wants. Returns a data URI, or None if it isn't a readable image."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGBA")
        size = (AVATAR_SIZE, AVATAR_SIZE) if kind == "avatar" else BANNER_SIZE
        img = ImageOps.fit(img, size, Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.info(f"[bot-profile] image rejected: {e}")
        return None


async def _read_image(upload: "discord.ui.FileUpload", url: str, kind: str):
    """Returns (data_uri, None) or (None, reason). Exactly one of upload/url must be given."""
    from discord_bot.cogs.welcome import (
        CUSTOM_BG_ALLOWED_CONTENT_TYPES, CUSTOM_BG_MAX_BYTES, _fetch_custom_bg_bytes,
    )
    files = list(upload.values or []) if upload is not None else []
    url = (url or "").strip()
    if files and url:
        return None, "use either an upload or a link, not both"
    if not files and not url:
        return None, None            # nothing given for this one, not an error
    if files:
        ct = (files[0].content_type or "").split(";")[0].strip().lower()
        if ct not in CUSTOM_BG_ALLOWED_CONTENT_TYPES or files[0].size > CUSTOM_BG_MAX_BYTES:
            return None, "use a png or jpg under 8MB"
        data = await files[0].read()
    else:
        async with aiohttp.ClientSession() as session:
            data, reason = await _fetch_custom_bg_bytes(session, url)
        if data is None:
            return None, reason or "couldn't fetch that link"
    uri = await asyncio.to_thread(_prepare_image, data, kind)
    if uri is None:
        return None, "couldn't read that image"
    return uri, None


# ── Discord calls ─────────────────────────────────────────────────────────

def _why(e: Exception) -> str:
    text = getattr(e, "text", None) or str(e)
    if isinstance(e, discord.Forbidden):
        return "I don't have permission to do that in this server."
    if isinstance(e, discord.HTTPException) and e.status == 429:
        return "Discord is rate-limiting profile changes. Try again in a little while."
    return f"Discord said no: {text[:180]}"


async def _patch_server_profile(client: discord.Client, guild_id: int, payload: dict):
    """PATCH /guilds/{id}/members/@me with only the keys in payload (None clears a field). Raw route so it works on
    any discord.py 2.x. Returns None on success or a human-readable reason."""
    try:
        route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
        await client.http.request(route, json=payload)
        return None
    except discord.HTTPException as e:
        return _why(e)
    except Exception as e:
        logger.warning(f"[bot-profile] server profile patch failed: {e}")
        return "something went wrong talking to Discord."


async def _sync_clone_username(clone_id, username: str) -> None:
    """Keep /myclones in step with the new global name (best effort)."""
    if clone_id is None:
        return
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE discord_cloned_bots SET bot_username = $1 WHERE clone_id = $2", username, clone_id)
    except Exception as e:
        logger.warning(f"[bot-profile] couldn't sync clone username: {e}")


# ── copy to the storage channel ───────────────────────────────────────────

async def _storage_channel(client: discord.Client):
    """The shared storage channel THIS bot can reach (DEV_STORAGE_CHANNEL_ID first, then the image-hosting channel), or None."""
    from modules import image_host
    try:
        import config
        cid = int(getattr(config, "DEV_STORAGE_CHANNEL_ID", 0) or 0)
        if cid:
            ch, _why_not = await image_host.open_channel(client, cid)
            if ch is not None:
                return ch
        return await image_host.resolve_channel(client)
    except Exception as e:
        logger.warning(f"[bot-profile] storage channel lookup failed: {e}")
        return None


async def archive_profile_image(interaction: discord.Interaction, guild_id: int, clone_id, kind: str, data_uri: str,
                                scope: str = "server") -> bool:
    """Send a copy of a just-applied avatar/banner to the storage channel with who/where/what. Never raises: the profile
    change already succeeded, so a storage problem is only logged. Returns True when the copy was posted."""
    try:
        channel = await _storage_channel(interaction.client)
        if channel is None:
            logger.warning("[bot-profile] no reachable storage channel, copy of %s skipped", kind)
            return False
        raw = base64.b64decode(data_uri.split(",", 1)[1])
        guild = interaction.client.get_guild(guild_id)
        user = interaction.user
        bot_name = getattr(getattr(interaction.client, "user", None), "name", "bot")
        info = (
            f"🖼️ **Bot profile {kind}** ({'global' if scope == 'global' else 'this server only'})\n"
            f"**Server:** {guild.name if guild else 'unknown'} (`{guild_id}`)\n"
            f"**Changed by:** {user} (`{user.id}`)\n"
            f"**Bot:** {bot_name}" + (f" (clone `{clone_id}`)" if clone_id is not None else " (main bot)") + "\n"
            f"**Time:** <t:{int(discord.utils.utcnow().timestamp())}:F>"
        )
        await channel.send(content=info[:1900], file=discord.File(io.BytesIO(raw), filename=f"{kind}_{guild_id}.png"),
                           allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception as e:
        logger.warning(f"[bot-profile] couldn't copy {kind} to the storage channel: {e}")
        return False


# ── vote gate: one DiscordThings vote unlocks the editor for the whole server ──
# Any admin's verified vote (GET /bots/{id}/check-vote) marks the server unlocked for good (bot_profile_vote_unlocks), so
# the vote is needed once per server, not once per person or per day. Reset and Done stay open. BOT_PROFILE_VOTE_GATE=0
# turns it off; no DiscordThings token configured also lets people through (a vote can't be checked without it).

async def _send_ephemeral(interaction: discord.Interaction, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=True, **kwargs)


async def _ensure_unlocked(interaction: discord.Interaction, guild_id: int, clone_id) -> bool:
    """True = this server may edit. False = the person was told to vote (or to retry); nothing else to do."""
    if not getattr(config, "BOT_PROFILE_VOTE_GATE", True):
        return True
    try:
        if await db.is_bot_profile_unlocked(guild_id):
            return True
    except Exception:
        logger.exception("[bot-profile] unlock lookup failed for guild %s", guild_id)
        return True                                   # a DB hiccup must not lock people out
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)   # the vote lookup can take a few seconds
    status = await dthings_vote.check_vote(interaction.user.id)
    if status == dthings_vote.VOTED:
        try:
            await db.unlock_bot_profile(guild_id, interaction.user.id)
        except Exception:
            logger.exception("[bot-profile] couldn't save the unlock for guild %s", guild_id)
        return True
    if status == dthings_vote.NO_TOKEN:
        logger.warning("[bot-profile] vote gate skipped: no DiscordThings token configured")
        return True
    if status == dthings_vote.ERROR:
        text = "⚠️ I couldn't reach DiscordThings to check your vote. Try again in a moment."
    else:
        text = ("🗳️ **Edit profile is unlocked for your whole server with one vote.** Vote for me on DiscordThings, "
                "then tap **I voted** and I'll check it. You only ever do this once per server.")
    view = discord.ui.View(timeout=None)
    if getattr(config, "DTHINGS_VOTE_URL", ""):
        view.add_item(discord.ui.Button(label="Vote on DiscordThings", emoji="🗳️", style=discord.ButtonStyle.link,
                                        url=config.DTHINGS_VOTE_URL))
    view.add_item(_VoteCheckButton(guild_id, clone_id))
    await _send_ephemeral(interaction, content=text, view=view)
    return False


# ── the panel ─────────────────────────────────────────────────────────────

# ── premium gate: set it up first, pay when you tap Save ───────────────────
# The editor is FREE for everyone right now. Flip this to True to bring back "set it up, pay when you tap Save"
# (everything below, the 30-minute draft, the Apply button and the pitch, is kept and tested for that).
PREMIUM_GATE = False
DRAFT_TTL = 30 * 60
_drafts: dict = {}            # (guild_id, user_id) -> {"fields": {...}, "exp": monotonic seconds}


def _draft_key(guild_id: int, user_id: int):
    return (int(guild_id), int(user_id))


def save_draft(guild_id: int, user_id: int, fields: dict) -> dict:
    """Merge `fields` (nick / bio / avatar / banner) into this admin's waiting draft and restart its 30 minutes."""
    now = time.monotonic()
    for k in [k for k, v in _drafts.items() if v["exp"] <= now]:
        _drafts.pop(k, None)
    cur = dict((_drafts.get(_draft_key(guild_id, user_id)) or {}).get("fields", {}))
    cur.update(fields)
    _drafts[_draft_key(guild_id, user_id)] = {"fields": cur, "exp": now + DRAFT_TTL}
    return cur


def get_draft(guild_id: int, user_id: int) -> dict:
    d = _drafts.get(_draft_key(guild_id, user_id))
    if not d or d["exp"] <= time.monotonic():
        _drafts.pop(_draft_key(guild_id, user_id), None)
        return {}
    return d["fields"]


def clear_draft(guild_id: int, user_id: int) -> None:
    _drafts.pop(_draft_key(guild_id, user_id), None)


def clear_draft_field(guild_id: int, user_id: int, key: str) -> None:
    d = _drafts.get(_draft_key(guild_id, user_id))
    if d:
        d["fields"].pop(key, None)
        if not d["fields"]:
            _drafts.pop(_draft_key(guild_id, user_id), None)


def _draft_summary(fields: dict) -> str:
    names = {"nick": "name", "bio": "about me", "avatar": "avatar", "banner": "banner", "ai_name": "AI name"}
    return ", ".join(names[k] for k in ("nick", "ai_name", "avatar", "banner", "bio") if k in fields)


async def is_premium(guild_id: int, clone_id) -> bool:
    try:
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        logger.warning("swallowed exception in is_premium", exc_info=True)
        return False


async def _pitch(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    try:
        from discord_bot.cogs._views_premium import send_premium_pitch
        await send_premium_pitch(interaction, guild_id, clone_id)
    except Exception:
        logger.exception("[bot-profile] couldn't show the Premium pitch for guild %s", guild_id)
        await interaction.followup.send("💎 Changing my look is a **Premium** feature. Run `/premium` to see the plans.",
                                        ephemeral=True)


async def _save_or_pitch(interaction: discord.Interaction, guild_id: int, clone_id, fields: dict) -> bool:
    """Call after interaction.response.defer(). True = Premium, go ahead and write. False = not Premium: the draft is
    kept for 30 minutes, the wizard shows it, and the Premium pitch with the pay buttons is sent."""
    if not PREMIUM_GATE or await is_premium(guild_id, clone_id):
        return True
    draft = save_draft(guild_id, interaction.user.id, fields)
    await _refresh(interaction, guild_id, clone_id,
                   f"💎 **Premium needed to save.** Your changes ({_draft_summary(draft)}) are kept for 30 minutes. "
                   "Pay below, then tap **✨ Apply my changes**.")
    await _pitch(interaction, guild_id, clone_id)
    return False


def _describe(guild: "discord.Guild | None") -> str:
    me = guild.me if guild else None
    if me is None:
        return "I couldn't load my profile in this server right now."
    nick = me.nick or "*(none, showing my normal name)*"
    avatar = "set" if getattr(me, "guild_avatar", None) else "default"
    return f"**Nickname:** {nick}\n**Server avatar:** {avatar}"


def build_profile_view(guild: "discord.Guild | None", guild_id: int, clone_id, owner: bool, note: str | None = None,
                       draft: dict | None = None, premium: bool | None = None):
    container = discord.ui.Container(accent_colour=discord.Color.blurple())
    tag = ("Free" if not PREMIUM_GATE else "💎 Premium" if premium else "🔒 Premium feature")
    container.add_item(discord.ui.TextDisplay(
        f"## 🤖 Bot profile · {tag}\nMake me look the way you want in your server: change my name, avatar, banner and "
        "about me, and name the AI that chats in your server. " + ("" if premium or not PREMIUM_GATE else "Set it all up first; you only pay when you tap Save. ") +
        "It only changes how I look here" + (", plus my global profile below since this is your bot." if owner else ".")))
    container.add_item(discord.ui.TextDisplay(_describe(guild)))
    if note:
        container.add_item(discord.ui.TextDisplay(note))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.ActionRow(
        _RenameButton(guild_id, clone_id), _AvatarButton(guild_id, clone_id),
        _BannerButton(guild_id, clone_id), _BioButton(guild_id, clone_id), _AiNameButton(guild_id, clone_id),
    ))
    row2 = [_ResetButton(guild_id, clone_id)]
    if draft:
        container.add_item(discord.ui.TextDisplay(f"📝 Waiting to apply: {_draft_summary(draft)}"))
        row2.insert(0, _ApplyButton(guild_id, clone_id))
    if owner:
        row2.insert(0, _GlobalButton(guild_id, clone_id))
    row2.append(_DoneButton(guild_id, clone_id))
    container.add_item(discord.ui.ActionRow(*row2))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


async def open_profile_wizard(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the More-features button (already deferred). Sends the wizard as its own message, like Card options."""
    if not await _ensure_unlocked(interaction, guild_id, clone_id):
        return                                        # told to vote on DiscordThings first
    guild = interaction.client.get_guild(guild_id)
    owner = await is_clone_owner(clone_id, interaction.user.id)
    view = build_profile_view(guild, guild_id, clone_id, owner, draft=get_draft(guild_id, interaction.user.id),
                              premium=await is_premium(guild_id, clone_id))
    await interaction.followup.send(view=view, ephemeral=interaction.guild is not None)


async def _refresh(interaction: discord.Interaction, guild_id: int, clone_id, note: str) -> None:
    """Edit the wizard message in place after a modal submit (modal submits that came from a button can edit it)."""
    guild = interaction.client.get_guild(guild_id)
    owner = await is_clone_owner(clone_id, interaction.user.id)
    view = build_profile_view(guild, guild_id, clone_id, owner, note=note, draft=get_draft(guild_id, interaction.user.id),
                              premium=await is_premium(guild_id, clone_id))
    try:
        await interaction.edit_original_response(view=view)
    except discord.HTTPException:
        await interaction.followup.send(note, ephemeral=True)


# ── modals ────────────────────────────────────────────────────────────────

class _NickModal(discord.ui.Modal, title="Rename me in this server"):
    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id, self.clone_id = guild_id, clone_id
        self.nick = discord.ui.TextInput(label="Nickname (blank = my normal name)", required=False, max_length=NICK_MAX)
        self.add_item(self.nick)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = str(self.nick.value or "").strip()
        if not await _save_or_pitch(interaction, self.guild_id, self.clone_id, {"nick": value or None}):
            return
        clear_draft_field(self.guild_id, interaction.user.id, "nick")
        err = await _patch_server_profile(interaction.client, self.guild_id, {"nick": value or None})
        if err:
            await interaction.followup.send(f"⚠️ Couldn't rename me: {err}", ephemeral=True)
            return
        await _refresh(interaction, self.guild_id, self.clone_id,
                       f"✅ I'm now called **{value}** here." if value else "✅ Nickname cleared.")


class _BioModal(discord.ui.Modal, title="My 'about me' in this server"):
    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id, self.clone_id = guild_id, clone_id
        self.bio = discord.ui.TextInput(label="About me (blank = clear it)", style=discord.TextStyle.paragraph,
                                        required=False, max_length=BIO_MAX)
        self.add_item(self.bio)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = str(self.bio.value or "").strip()
        if not await _save_or_pitch(interaction, self.guild_id, self.clone_id, {"bio": value or None}):
            return
        clear_draft_field(self.guild_id, interaction.user.id, "bio")
        err = await _patch_server_profile(interaction.client, self.guild_id, {"bio": value or None})
        if err:
            await interaction.followup.send(f"⚠️ Couldn't update my bio: {err}", ephemeral=True)
            return
        await _refresh(interaction, self.guild_id, self.clone_id, "✅ Bio updated." if value else "✅ Bio cleared.")


class _ImageModal(discord.ui.Modal):
    """Avatar or banner for this server: upload an image or paste a direct link (same two inputs as the card background)."""

    def __init__(self, guild_id: int, clone_id, kind: str):
        super().__init__(title="Server avatar" if kind == "avatar" else "Server banner")
        self.guild_id, self.clone_id, self.kind = guild_id, clone_id, kind
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.url = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=500,
                                        placeholder="https://…/image.png")
        self.add_item(discord.ui.Label(text="Upload an image", description="png or jpg, up to 8MB", component=self.upload))
        self.add_item(discord.ui.Label(text="…or paste a direct image link", description="Fill in only one of the two",
                                       component=self.url))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uri, reason = await _read_image(self.upload, str(self.url.value or ""), self.kind)
        if uri is None:
            await interaction.followup.send(
                f"⚠️ {('Couldn' + chr(39) + 't use that image: ' + reason) if reason else 'Upload an image or paste a link first.'}",
                ephemeral=True)
            return
        if not await _save_or_pitch(interaction, self.guild_id, self.clone_id, {self.kind: uri}):
            return
        clear_draft_field(self.guild_id, interaction.user.id, self.kind)
        err = await _patch_server_profile(interaction.client, self.guild_id, {self.kind: uri})
        if err:
            await interaction.followup.send(f"⚠️ Couldn't change my {self.kind}: {err}", ephemeral=True)
            return
        await archive_profile_image(interaction, self.guild_id, self.clone_id, self.kind, uri, "server")
        await _refresh(interaction, self.guild_id, self.clone_id, f"✅ New {self.kind} set for this server.")


class _AiNameModal(discord.ui.Modal, title="Name the AI in this server"):
    """Premium like the rest of the editor: a free server sets it up, then pays when it taps Save."""

    def __init__(self, guild_id: int, clone_id, current: str | None = None):
        super().__init__()
        self.guild_id, self.clone_id = guild_id, clone_id
        self.name = discord.ui.TextInput(label="AI name (blank = default)", required=False, default=current,
                                         max_length=ai_name.NAME_MAX, placeholder="e.g. Luna")
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value, problem = ai_name.clean_name(str(self.name.value or ""))
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return
        if not await _save_or_pitch(interaction, self.guild_id, self.clone_id, {"ai_name": value}):
            return
        clear_draft_field(self.guild_id, interaction.user.id, "ai_name")
        if not await ai_name.set_name(self.guild_id, value, interaction.user.id):
            await interaction.followup.send("⚠️ Couldn't save the AI's name right now. Try again in a moment.",
                                            ephemeral=True)
            return
        await _refresh(interaction, self.guild_id, self.clone_id,
                       f"✅ The AI here is now called **{value}**." if value else "✅ The AI is back to its default name.")


class _GlobalModal(discord.ui.Modal, title="My global profile (bot owner)"):
    """Clone owners only: the bot's real username/avatar/banner on Discord. Leave a field blank to keep it."""

    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id, self.clone_id = guild_id, clone_id
        self.username = discord.ui.TextInput(required=False, max_length=32, min_length=0)
        self.avatar = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.banner = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(discord.ui.Label(text="New username", description="2-32 characters. Discord allows about 2 changes an hour",
                                       component=self.username))
        self.add_item(discord.ui.Label(text="New avatar", description="png or jpg, up to 8MB", component=self.avatar))
        self.add_item(discord.ui.Label(text="New banner", description="png or jpg, up to 8MB", component=self.banner))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await is_clone_owner(self.clone_id, interaction.user.id):      # re-check: modals outlive the panel
            await interaction.followup.send("⚠️ Only this bot's owner can change its global profile.", ephemeral=True)
            return
        changes = {}
        uris = {}
        name = str(self.username.value or "").strip()
        if name:
            if len(name) < 2:
                await interaction.followup.send("⚠️ A username needs at least 2 characters.", ephemeral=True)
                return
            changes["username"] = name
        for kind, field in (("avatar", self.avatar), ("banner", self.banner)):
            uri, reason = await _read_image(field, "", kind)
            if reason:
                await interaction.followup.send(f"⚠️ Couldn't use that {kind}: {reason}.", ephemeral=True)
                return
            if uri:
                changes[kind] = uri
                uris[kind] = uri
        if not changes:
            await interaction.followup.send("⚠️ Nothing to change. Fill in at least one field.", ephemeral=True)
            return
        try:
            route = discord.http.Route("PATCH", "/users/@me")
            await interaction.client.http.request(route, json=changes)
        except discord.HTTPException as e:
            await interaction.followup.send(f"⚠️ Couldn't change my global profile: {_why(e)}", ephemeral=True)
            return
        for kind, uri in uris.items():
            await archive_profile_image(interaction, self.guild_id, self.clone_id, kind, uri, "global")
        if "username" in changes:
            await _sync_clone_username(self.clone_id, changes["username"])
        await _refresh(interaction, self.guild_id, self.clone_id,
                       "✅ Global profile updated (" + ", ".join(sorted(changes)) + "). It can take a minute to show everywhere.")


# ── buttons ───────────────────────────────────────────────────────────────

class _Btn:
    action = ""
    label = ""
    emoji = None
    style = discord.ButtonStyle.secondary
    gated = True          # needs the server's DiscordThings vote; Reset / Done / the vote check itself stay open

    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label=self.label, emoji=self.emoji, style=self.style, custom_id=_cid(self.action, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        # Every action needs Manage Server in the target server; modals and edits come after this gate.
        if not await _check_access(interaction, self.guild_id):
            return
        if self.gated:
            if not await _ensure_unlocked(interaction, self.guild_id, self.clone_id):
                return
            if interaction.response.is_done():        # the vote was just verified on this tap; modals need a fresh one
                await interaction.followup.send("✅ Thanks for voting! Edit profile is unlocked for this server. "
                                                "Tap that button again.", ephemeral=True)
                return
        await self.act(interaction)

    async def act(self, interaction: discord.Interaction):  # pragma: no cover
        raise NotImplementedError


class _RenameButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("name")):
    action, label, emoji = "name", "Change name", "✏️"
    style = discord.ButtonStyle.primary

    async def act(self, interaction):
        await interaction.response.send_modal(_NickModal(self.guild_id, self.clone_id))


class _AvatarButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("avatar")):
    action, label, emoji = "avatar", "Avatar", "🖼️"

    async def act(self, interaction):
        await interaction.response.send_modal(_ImageModal(self.guild_id, self.clone_id, "avatar"))


class _BannerButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("banner")):
    action, label, emoji = "banner", "Banner", "🌄"

    async def act(self, interaction):
        await interaction.response.send_modal(_ImageModal(self.guild_id, self.clone_id, "banner"))


class _BioButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("bio")):
    action, label, emoji = "bio", "About me", "📝"

    async def act(self, interaction):
        await interaction.response.send_modal(_BioModal(self.guild_id, self.clone_id))


class _AiNameButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("ainame")):
    action, label, emoji = "ainame", "AI name", "🧠"

    async def act(self, interaction):
        current = await ai_name.get_name(self.guild_id)
        await interaction.response.send_modal(_AiNameModal(self.guild_id, self.clone_id, current))


class _GlobalButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("global")):
    action, label, emoji = "global", "Global profile", "🌐"
    style = discord.ButtonStyle.success

    async def act(self, interaction):
        if not await is_clone_owner(self.clone_id, interaction.user.id):
            await interaction.response.send_message("Only this bot's owner can change its global profile.", ephemeral=True)
            return
        await interaction.response.send_modal(_GlobalModal(self.guild_id, self.clone_id))


class _ApplyButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("apply")):
    action, label, emoji = "apply", "Apply my changes", "✨"
    style = discord.ButtonStyle.success

    async def act(self, interaction):
        await interaction.response.defer()
        fields = dict(get_draft(self.guild_id, interaction.user.id))
        if not fields:
            await interaction.followup.send("Nothing is waiting any more (drafts last 30 minutes). Make your changes again.",
                                            ephemeral=True)
            return
        if PREMIUM_GATE and not await is_premium(self.guild_id, self.clone_id):   # re-checked: a stale button can't write
            await _refresh(interaction, self.guild_id, self.clone_id,
                           "💎 Still waiting for Premium. Your draft is kept for 30 minutes.")
            await _pitch(interaction, self.guild_id, self.clone_id)
            return
        summary = _draft_summary(fields)
        ai_given = "ai_name" in fields
        new_ai_name = fields.pop("ai_name", None)
        if fields:
            err = await _patch_server_profile(interaction.client, self.guild_id, fields)
            if err:
                await interaction.followup.send(f"⚠️ Couldn't apply your changes: {err}", ephemeral=True)
                return
        if ai_given and not await ai_name.set_name(self.guild_id, new_ai_name, interaction.user.id):
            await interaction.followup.send("⚠️ Your other changes are in, but I couldn't save the AI's name. "
                                            "Tap **AI name** to try again.", ephemeral=True)
            clear_draft(self.guild_id, interaction.user.id)
            return
        for kind in ("avatar", "banner"):
            if fields.get(kind):
                await archive_profile_image(interaction, self.guild_id, self.clone_id, kind, fields[kind], "server")
        clear_draft(self.guild_id, interaction.user.id)
        await _refresh(interaction, self.guild_id, self.clone_id, f"✅ Applied: {summary}.")


class _ResetButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("reset")):
    action, label, emoji = "reset", "Reset to default", "♻️"
    style = discord.ButtonStyle.danger
    gated = False

    async def act(self, interaction):
        await interaction.response.defer()
        err = await _patch_server_profile(
            interaction.client, self.guild_id, {"nick": None, "avatar": None, "banner": None, "bio": None})
        if err:
            await interaction.followup.send(f"⚠️ Couldn't reset my profile: {err}", ephemeral=True)
            return
        await ai_name.set_name(self.guild_id, None, interaction.user.id)       # the AI goes back to its default name too
        await _refresh(interaction, self.guild_id, self.clone_id, "✅ Back to my default look (and the AI's default name) here.")


class _DoneButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("done")):
    action, label, emoji = "done", "Done", "✅"
    style = discord.ButtonStyle.success
    gated = False

    async def act(self, interaction):
        await interaction.response.defer()
        container = discord.ui.Container(accent_colour=discord.Color.green())
        container.add_item(discord.ui.TextDisplay("✅ Bot profile saved. Run `/start` any time to open this again."))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await interaction.edit_original_response(view=view)


class _VoteCheckButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_pat("votecheck")):
    action, label, emoji = "votecheck", "I voted", "✅"
    style = discord.ButtonStyle.success
    gated = False

    async def act(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if await _ensure_unlocked(interaction, self.guild_id, self.clone_id):
            await open_profile_wizard(interaction, self.guild_id, self.clone_id)


DYNAMIC_ITEMS = (_RenameButton, _AvatarButton, _BannerButton, _BioButton, _AiNameButton, _GlobalButton, _ApplyButton,
                 _ResetButton,
                 _DoneButton, _VoteCheckButton)
