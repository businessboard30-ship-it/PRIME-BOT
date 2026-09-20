# path: discord_bot/cogs/_views_connect.py

"""
/connections wizard — the ONE command; everything else is buttons, selects and
modals inside one ephemeral message (only the person who ran it sees it):

  Hub
   ├─ 👤 My accounts   link / verify / unlink YouTube + Roblox (code method)
   ├─ ▶️ YouTube       video / channel / playlist info, trending by country, random video
   ├─ 🎮 Roblox        user / game / group info
   └─ 🔔 Notifications (Manage Server) YouTube upload feeds, Roblox game-update
                       feeds, and the Roblox-verified role

Results are ephemeral rich embeds with a "Share in channel" button.
Views here are plain (timeout 15 min) rather than DynamicItems: the wizard is
ephemeral, so nothing outlives the message anyway. Notification posts have no
components, so they survive restarts on their own.
"""

import logging

import discord

from discord_bot.cogs import _connect_core as core
from modules import connections_api as api
from modules.connections_api import ConnectError

logger = logging.getLogger(__name__)

WIZARD_TIMEOUT = 900


class Ctx:
    def __init__(self, invoker_id: int, guild, clone_id):
        self.invoker_id = invoker_id
        self.guild = guild            # discord.Guild | None (DMs / user-installs)
        self.clone_id = clone_id


# ── small helpers ─────────────────────────────────────────────────────────

def _button(label, cb, style=discord.ButtonStyle.secondary, emoji=None) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji)
    b.callback = cb
    return b


class _Page(discord.ui.LayoutView):
    """One wizard page: header text + rows of components."""

    def __init__(self, ctx: Ctx, title: str, lines: list, rows: list, accent: discord.Color):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.ctx = ctx
        container = discord.ui.Container(accent_colour=accent)
        container.add_item(discord.ui.TextDisplay("\n".join([f"### {title}", *lines])))
        container.add_item(discord.ui.Separator())
        for items in rows:
            row = discord.ui.ActionRow()
            for it in items:
                row.add_item(it)
            container.add_item(row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.invoker_id:
            await interaction.response.send_message("This menu belongs to whoever ran `/connections`.", ephemeral=True)
            return False
        return True


async def _goto(interaction: discord.Interaction, ctx: Ctx, builder) -> None:
    """Swap the wizard message to another page (builder may be async)."""
    await interaction.response.defer()
    view = builder(ctx)
    if hasattr(view, "__await__"):
        view = await view
    await interaction.edit_original_response(view=view)


def _can_manage(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.permissions.manage_guild)


class _ShareView(discord.ui.View):
    def __init__(self, embed: discord.Embed, user: discord.abc.User):
        super().__init__(timeout=300)
        self.embed = embed
        self.user = user

    @discord.ui.button(label="Share in channel", emoji="📢", style=discord.ButtonStyle.primary)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        if ch is None:
            await interaction.response.send_message("I can't post in this chat.", ephemeral=True)
            return
        try:
            await ch.send(content=f"Shared by {self.user.mention}", embed=self.embed,
                          allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.response.send_message("I can't post in this channel.", ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)


async def _send_result(interaction: discord.Interaction, embed: discord.Embed) -> None:
    await interaction.followup.send(embed=embed, view=_ShareView(embed, interaction.user), ephemeral=True)


class _TextModal(discord.ui.Modal):
    def __init__(self, title, label, placeholder, handler, description=None):
        super().__init__(title=title)
        self.handler = handler
        self.inp = discord.ui.TextInput(style=discord.TextStyle.short, max_length=200, placeholder=placeholder)
        self.add_item(discord.ui.Label(text=label, description=description, component=self.inp))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.handler(interaction, str(self.inp.value or "").strip())
        except ConnectError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except Exception:
            logger.exception("[connect] lookup failed")
            await interaction.followup.send("⚠️ Something went wrong — try again.", ephemeral=True)


def _ask(title, label, placeholder, handler, description=None):
    async def cb(interaction: discord.Interaction):
        await interaction.response.send_modal(_TextModal(title, label, placeholder, handler, description))
    return cb


# ══ lookups ═══════════════════════════════════════════════════════════════

async def _do_video(interaction, text):
    v = await api.get_video(text)
    if not v:
        raise ConnectError("No video found for that.")
    await _send_result(interaction, core.video_embed(v))


async def _do_channel(interaction, text):
    c = await api.get_channel(text)
    if not c:
        raise ConnectError("No channel found for that.")
    await _send_result(interaction, core.channel_embed(c))


async def _do_playlist(interaction, text):
    p = await api.get_playlist(text)
    if not p:
        raise ConnectError("No playlist found — paste a playlist link (it contains `list=`).")
    await _send_result(interaction, core.playlist_embed(p))


async def _do_trending(interaction, region):
    region = region.strip().upper()
    vids = await api.get_trending(region)
    label = api.TRENDING_REGIONS.get(region, region)
    await _send_result(interaction, core.trending_embed(label, region, vids))


async def _do_random(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        v = await api.get_random_video()
        if not v:
            raise ConnectError("Couldn't find a video this time — try again.")
        await _send_result(interaction, core.random_embed(v))
    except ConnectError as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)


async def _do_rbx_user(interaction, text):
    u = await api.rbx_get_user(text)
    if not u:
        raise ConnectError("No Roblox user found for that.")
    await _send_result(interaction, core.roblox_user_embed(u))


async def _do_rbx_game(interaction, text):
    g = await api.rbx_get_game(text)
    if not g:
        raise ConnectError("No Roblox game found for that link.")
    await _send_result(interaction, core.roblox_game_embed(g))


async def _do_rbx_group(interaction, text):
    g = await api.rbx_get_group(text)
    if not g:
        raise ConnectError("No Roblox group found for that.")
    await _send_result(interaction, core.roblox_group_embed(g))


# ══ pages ═════════════════════════════════════════════════════════════════

def build_hub(ctx: Ctx) -> discord.ui.LayoutView:
    lines = [
        "Link your accounts, look things up, and get notified — all from here.",
        "👤 **My accounts** — verify your YouTube channel and Roblox account",
        "▶️ **YouTube** — video, channel & playlist info · trending by country · random video",
        "🎮 **Roblox** — user, game & group info",
    ]
    if ctx.guild:
        lines.append("🔔 **Notifications** — new-upload and game-update feeds *(Manage Server)*")
    if not api.youtube_configured():
        lines.append("-# ⚠️ YouTube lookups are off until the bot owner sets `YOUTUBE_API_KEY`.")

    async def close(interaction):
        await interaction.response.defer()
        await interaction.delete_original_response()

    row1 = [
        _button("My accounts", lambda i: _goto(i, ctx, build_accounts), discord.ButtonStyle.primary, "👤"),
        _button("YouTube", lambda i: _goto(i, ctx, build_youtube), discord.ButtonStyle.secondary, "▶️"),
        _button("Roblox", lambda i: _goto(i, ctx, build_roblox), discord.ButtonStyle.secondary, "🎮"),
    ]
    row2 = []
    if ctx.guild:
        async def notif(interaction):
            if not _can_manage(interaction):
                await interaction.response.send_message("You need the **Manage Server** permission for Notifications.", ephemeral=True)
                return
            await _goto(interaction, ctx, build_notifications)
        row2.append(_button("Notifications", notif, discord.ButtonStyle.secondary, "🔔"))
    row2.append(_button("Close", close, discord.ButtonStyle.danger, "✖️"))
    return _Page(ctx, "🔌 Connect", lines, [row1, row2], discord.Color.blurple())


def _back(ctx: Ctx) -> discord.ui.Button:
    return _button("Back", lambda i: _goto(i, ctx, build_hub), discord.ButtonStyle.secondary, "⬅️")


def build_youtube(ctx: Ctx) -> discord.ui.LayoutView:
    lines = ["Detailed, live YouTube info in a clean card. Every result has a **Share** button."]
    if not api.youtube_configured():
        lines.append("-# ⚠️ Needs `YOUTUBE_API_KEY` on the bot.")
    r1 = [
        _button("Video info", _ask("Video info", "Video link or search", "https://youtu.be/… or a title", _do_video), discord.ButtonStyle.primary, "🎬"),
        _button("Channel info", _ask("Channel info", "Channel link, @handle or name", "@MrBeast", _do_channel), discord.ButtonStyle.primary, "📺"),
        _button("Playlist info", _ask("Playlist info", "Playlist link", "https://youtube.com/playlist?list=…", _do_playlist), discord.ButtonStyle.primary, "📑"),
    ]
    r2 = [
        _button("Trending", lambda i: _goto(i, ctx, build_trending), discord.ButtonStyle.success, "🔥"),
        _button("Random video", _do_random, discord.ButtonStyle.success, "🎲"),
        _back(ctx),
    ]
    return _Page(ctx, "▶️ YouTube", lines, [r1, r2], core.YT_RED)


def build_trending(ctx: Ctx) -> discord.ui.LayoutView:
    OTHER = "__other__"
    regions = list(api.TRENDING_REGIONS.items())[:24]
    options = [discord.SelectOption(label=name, value=code, description=code) for code, name in regions]
    options.append(discord.SelectOption(label="Other country…", value=OTHER, emoji="🌍", description="Type a 2-letter code"))
    sel = discord.ui.Select(placeholder="Pick a country", options=options)

    async def picked(interaction: discord.Interaction):
        val = sel.values[0]
        if val == OTHER:
            await interaction.response.send_modal(
                _TextModal("Trending — other country", "2-letter country code", "GH, US, JP…", _do_trending)
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await _do_trending(interaction, val)
        except ConnectError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

    sel.callback = picked
    back = _button("Back", lambda i: _goto(i, ctx, build_youtube), discord.ButtonStyle.secondary, "⬅️")
    return _Page(ctx, "🔥 Trending", ["Pick a country to see its top 10 trending videos right now."],
                 [[sel], [back]], core.YT_RED)


def build_roblox(ctx: Ctx) -> discord.ui.LayoutView:
    r1 = [
        _button("User info", _ask("Roblox user", "Username, ID or profile link", "Roblox", _do_rbx_user), discord.ButtonStyle.primary, "🧑"),
        _button("Game info", _ask("Roblox game", "Game link or place ID", "https://www.roblox.com/games/…", _do_rbx_game), discord.ButtonStyle.primary, "🕹️"),
        _button("Group info", _ask("Roblox group", "Group link or ID", "https://www.roblox.com/communities/…", _do_rbx_group), discord.ButtonStyle.primary, "🏰"),
    ]
    return _Page(ctx, "🎮 Roblox", ["Live player counts, visits, members and profile details. Every result has a **Share** button."],
                 [r1, [_back(ctx)]], core.RBX_BLUE)


# ══ account linking ═══════════════════════════════════════════════════════

PLATFORMS = {
    "youtube": {"label": "YouTube", "emoji": "▶️", "where": "your **channel description** (YouTube Studio → Customization → Basic info → Description → Publish)"},
    "roblox": {"label": "Roblox", "emoji": "🎮", "where": "your **profile About / description** on roblox.com"},
}


async def build_accounts(ctx: Ctx) -> discord.ui.LayoutView:
    links = await core.get_links(ctx.invoker_id)
    lines = ["Prove an account is yours by placing a one-time code in its public description. No passwords, no logins."]
    r1, r2 = [], []
    for key, meta in PLATFORMS.items():
        link = links.get(key)
        if link:
            url = (f"https://www.youtube.com/channel/{link['external_id']}" if key == "youtube"
                   else f"https://www.roblox.com/users/{link['external_id']}/profile")
            lines.append(f"{meta['emoji']} **{meta['label']}:** ✅ [{link['external_name']}]({url})")
            r2.append(_button(f"Unlink {meta['label']}", _unlinker(ctx, key), discord.ButtonStyle.danger, "🗑️"))
        else:
            lines.append(f"{meta['emoji']} **{meta['label']}:** not linked")
            r1.append(_button(f"Link {meta['label']}", _link_starter(ctx, key), discord.ButtonStyle.success, "🔗"))
    if "roblox" in links and ctx.guild:
        rid = await core.get_roblox_role(ctx.guild.id, ctx.clone_id)
        if rid:
            lines.append(f"-# Verified Roblox members get <@&{rid}> in this server.")
    rows = [r for r in (r1, r2) if r] + [[_back(ctx)]]
    return _Page(ctx, "👤 My accounts", lines, rows, discord.Color.blurple())


def _link_starter(ctx: Ctx, platform: str):
    async def handler(interaction: discord.Interaction, text: str):
        if platform == "youtube":
            c = await api.get_channel(text)
            if not c:
                raise ConnectError("No YouTube channel found for that.")
            ext_id, ext_name = c["id"], c["title"]
        else:
            u = await api.rbx_get_user(text)
            if not u:
                raise ConnectError("No Roblox user found for that.")
            ext_id, ext_name = str(u["id"]), u["name"]
        code = core.new_code()
        await core.set_pending(ctx.invoker_id, platform, ext_id, ext_name, code)
        meta = PLATFORMS[platform]
        msg = (
            f"{meta['emoji']} **Linking {meta['label']} account: {ext_name}**\n"
            f"1. Put this code in {meta['where']}:\n```{code}```\n"
            f"2. Tap **Verify** below (YouTube can take a few minutes to show the change).\n"
            f"-# You can remove the code again as soon as you're verified."
        )
        await interaction.followup.send(msg, view=_VerifyView(ctx, platform), ephemeral=True)

    label = "YouTube channel link, @handle or name" if platform == "youtube" else "Roblox username or ID"
    ph = "@yourchannel" if platform == "youtube" else "YourRobloxName"
    return _ask(f"Link {PLATFORMS[platform]['label']}", label, ph, handler)


class _VerifyView(discord.ui.View):
    def __init__(self, ctx: Ctx, platform: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.ctx = ctx
        self.platform = platform

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.invoker_id:
            await interaction.response.send_message("Not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pend = await core.get_pending(self.ctx.invoker_id, self.platform)
        if not pend:
            await interaction.followup.send("That link request expired — start again from **My accounts**.", ephemeral=True)
            return
        try:
            if self.platform == "youtube":
                desc = await api.get_channel_description(pend["external_id"])
            else:
                desc = await api.rbx_user_description(int(pend["external_id"]))
        except ConnectError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        if desc is None or pend["code"].lower() not in desc.lower():
            await interaction.followup.send(
                f"Couldn't find `{pend['code']}` in the description yet. Save it there, wait a moment, and tap Verify again.",
                ephemeral=True,
            )
            return
        await core.save_link(self.ctx.invoker_id, self.platform, pend["external_id"], pend["external_name"])
        note = ""
        if self.platform == "roblox" and self.ctx.guild:
            if await sync_roblox_role(self.ctx.guild, self.ctx.invoker_id, self.ctx.clone_id, add=True):
                note = "\n🎭 You got the server's Roblox-verified role."
        self.stop()
        await interaction.edit_original_response(
            content=f"✅ **{PLATFORMS[self.platform]['label']} linked** as **{pend['external_name']}**.{note}", view=None,
        )


def _unlinker(ctx: Ctx, platform: str):
    async def cb(interaction: discord.Interaction):
        await interaction.response.defer()
        await core.remove_link(ctx.invoker_id, platform)
        if platform == "roblox" and ctx.guild:
            await sync_roblox_role(ctx.guild, ctx.invoker_id, ctx.clone_id, add=False)
        await interaction.edit_original_response(view=await build_accounts(ctx))
    return cb


async def sync_roblox_role(guild: discord.Guild, user_id: int, clone_id, add: bool) -> bool:
    """Give/remove the server's Roblox-verified role. Best effort; True if
    the role was added."""
    try:
        rid = await core.get_roblox_role(guild.id, clone_id)
        if not rid:
            return False
        role = guild.get_role(int(rid))
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if role is None or member is None:
            return False
        if add:
            await member.add_roles(role, reason="Roblox account verified via /connections")
            return True
        await member.remove_roles(role, reason="Roblox account unlinked via /connections")
    except discord.HTTPException as e:
        logger.debug(f"[connect] role sync skipped in {guild.id}: {e}")
    return False


# ══ notifications (admin) ═════════════════════════════════════════════════

FEED_LABEL = {"yt": ("▶️", "YouTube uploads"), "rbx_game": ("🎮", "Roblox game updates")}


async def build_notifications(ctx: Ctx) -> discord.ui.LayoutView:
    guild = ctx.guild
    feeds = await core.list_feeds(guild.id, ctx.clone_id)
    rid = await core.get_roblox_role(guild.id, ctx.clone_id)
    lines = ["New uploads and game updates get posted to a channel of your choice (checked every ~10 minutes)."]
    if feeds:
        for f in feeds:
            emoji, kind = FEED_LABEL.get(f["kind"], ("🔔", f["kind"]))
            ping = f" · pings <@&{f['role_id']}>" if f["role_id"] else ""
            lines.append(f"{emoji} **{core.clip(f['external_name'], 50)}** ({kind}) → <#{f['channel_id']}>{ping}")
    else:
        lines.append("*No feeds yet.*")
    lines.append(f"🎭 **Roblox-verified role:** {f'<@&{rid}>' if rid else 'not set'}")
    lines.append(f"-# {len(feeds)}/{core.MAX_FEEDS_PER_GUILD} feeds used.")

    rows = []
    if feeds:
        opts = [
            discord.SelectOption(
                label=core.clip(f"{FEED_LABEL.get(f['kind'], ('', f['kind']))[1]}: {f['external_name']}", 100),
                value=str(f["id"]),
            )
            for f in feeds
        ]
        rm = discord.ui.Select(placeholder="🗑️ Remove a feed…", options=opts)

        async def removed(interaction: discord.Interaction):
            if not _can_manage(interaction):
                await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
                return
            await interaction.response.defer()
            await core.remove_feed(int(rm.values[0]), guild.id)
            await interaction.edit_original_response(view=await build_notifications(ctx))

        rm.callback = removed
        rows.append([rm])

    role_sel = discord.ui.RoleSelect(placeholder="🎭 Role for Roblox-verified members", min_values=1, max_values=1)

    async def role_picked(interaction: discord.Interaction):
        if not _can_manage(interaction):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        role = role_sel.values[0]
        me = interaction.guild.me
        if role.is_default() or role.managed or role >= me.top_role or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I can't hand out that role — it must be a normal role **below my top role**, and I need **Manage Roles**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await core.set_roblox_role(guild.id, ctx.clone_id, role.id)
        await interaction.edit_original_response(view=await build_notifications(ctx))

    role_sel.callback = role_picked
    rows.append([role_sel])

    async def clear_role(interaction: discord.Interaction):
        if not _can_manage(interaction):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        await interaction.response.defer()
        await core.set_roblox_role(guild.id, ctx.clone_id, None)
        await interaction.edit_original_response(view=await build_notifications(ctx))

    async def open_add(interaction: discord.Interaction, kind: str):
        if not _can_manage(interaction):
            await interaction.response.send_message("You need **Manage Server**.", ephemeral=True)
            return
        await interaction.response.send_modal(_AddFeedModal(ctx, kind))

    rows.append([
        _button("Add YouTube channel", lambda i: open_add(i, "yt"), discord.ButtonStyle.success, "➕"),
        _button("Add Roblox game", lambda i: open_add(i, "rbx_game"), discord.ButtonStyle.success, "➕"),
        _button("Clear role", clear_role, discord.ButtonStyle.secondary, "🧹"),
        _back(ctx),
    ])
    return _Page(ctx, "🔔 Notifications", lines, rows, discord.Color.gold())


class _AddFeedModal(discord.ui.Modal):
    def __init__(self, ctx: Ctx, kind: str):
        super().__init__(title="New YouTube feed" if kind == "yt" else "New Roblox game feed")
        self.ctx = ctx
        self.kind = kind
        self.link = discord.ui.TextInput(
            style=discord.TextStyle.short, max_length=200,
            placeholder="@handle, channel link or ID" if kind == "yt" else "https://www.roblox.com/games/…",
        )
        self.channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news], min_values=1, max_values=1,
            placeholder="Where should posts go?",
        )
        self.role = discord.ui.RoleSelect(min_values=0, max_values=1, required=False, placeholder="Optional: role to ping")
        self.add_item(discord.ui.Label(
            text="YouTube channel" if kind == "yt" else "Roblox game link", component=self.link))
        self.add_item(discord.ui.Label(text="Post in channel", component=self.channel))
        self.add_item(discord.ui.Label(text="Ping role", component=self.role))

    async def on_submit(self, interaction: discord.Interaction):
        # Non-thinking defer so we can refresh the wizard message underneath.
        await interaction.response.defer()
        guild = interaction.guild
        if not _can_manage(interaction) or guild is None:
            await interaction.followup.send("You need **Manage Server**.", ephemeral=True)
            return
        try:
            try:
                from database import db as _db
                premium = bool(await _db.is_guild_premium_active(guild.id, self.ctx.clone_id))
            except Exception:
                premium = False
            if not premium:
                await interaction.followup.send(
                    "🔔 **Notification feeds are a Premium feature.** Upgrade this server to unlock them.",
                    ephemeral=True,
                )
                from discord_bot.cogs._views_premium import send_premium_pitch
                await send_premium_pitch(interaction, guild.id, self.ctx.clone_id)
                return
            chan = guild.get_channel(self.channel.values[0].id)
            perms = chan.permissions_for(guild.me) if chan else None
            if chan is None or not (perms.view_channel and perms.send_messages and perms.embed_links):
                raise ConnectError("I need **View Channel, Send Messages and Embed Links** in that channel.")
            role_id = self.role.values[0].id if self.role.values else None
            text = str(self.link.value or "").strip()
            if self.kind == "yt":
                c = await api.get_channel(text)
                if not c:
                    raise ConnectError("No YouTube channel found for that.")
                try:
                    entries = await api.fetch_channel_feed(c["id"])
                except ConnectError:
                    entries = []
                state = {"last": entries[0]["published"]} if entries else {}
                ext_id, ext_name = c["id"], c["title"]
            else:
                g = await api.rbx_get_game(text)
                if not g:
                    raise ConnectError("No Roblox game found for that link.")
                state = {"updated": g["updated"], "place": g["place"]}
                ext_id, ext_name = str(g["universe"]), g["name"]
            try:
                fid = await core.add_feed(guild.id, self.ctx.clone_id, self.kind, ext_id, ext_name,
                                          chan.id, role_id, state, interaction.user.id)
            except ValueError:
                raise ConnectError(f"You've reached the limit of {core.MAX_FEEDS_PER_GUILD} feeds — remove one first.")
            if fid is None:
                raise ConnectError("That feed is already set up in this server.")
            await interaction.followup.send(f"✅ Now watching **{ext_name}** → {chan.mention}.", ephemeral=True)
        except ConnectError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except Exception:
            logger.exception("[connect] add feed failed")
            await interaction.followup.send("⚠️ Something went wrong — try again.", ephemeral=True)
        try:
            await interaction.edit_original_response(view=await build_notifications(self.ctx))
        except discord.HTTPException:
            pass


async def open_hub(interaction: discord.Interaction) -> None:
    ctx = Ctx(interaction.user.id, interaction.guild, getattr(interaction.client, "clone_id", None))
    await interaction.response.send_message(view=build_hub(ctx), ephemeral=True)
