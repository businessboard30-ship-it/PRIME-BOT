"""
Lets a user connect a media server or cloud folder THEY own, then search
and play from it. This bot is a search/UI layer only — it never stores,
caches, or proxies video content. Every /movie play link resolves
directly against the user's own Jellyfin server or their own Google
Drive folder; playback flows user's-storage -> user's-device.

Two connectors:
  - Jellyfin: user supplies their own server URL + API key
  - Google Drive: OAuth-connected, scoped to a single app-created folder
    (drive.file scope — not their whole Drive)

No direct file upload or arbitrary external-URL ingestion is offered
here on purpose — see modules/jellyfin_client.py and
modules/gdrive_client.py docstrings for why.
"""

import functools
import logging
import secrets as secrets_module

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from config import MEDIA_CONNECT_FEE_GHS
from payments import paystack
from modules import jellyfin_client, gdrive_client, plex_client
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button

logger = logging.getLogger(__name__)


def _require_subscription(func):
    """Gate a command behind an active Media Connect subscription.
    Discord clone admins (DISCORD_CLONE_ADMIN_IDS) always pass, same
    owner-bypass convention as image_search.py."""
    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        await interaction.response.defer(ephemeral=True)
        from config import DISCORD_CLONE_ADMIN_IDS
        if interaction.user.id in DISCORD_CLONE_ADMIN_IDS:
            await func(self, interaction, *args, **kwargs)
            return
        if not await db.is_media_connect_active(interaction.user.id):
            buttons = [ActionButton("Subscribe — $2/mo", discord.ButtonStyle.success, self, "subscribe", emoji="💳")]
            card = NavCardView("Media Connect", [
                "Media Connect (Jellyfin/Plex/Drive movie search) is a $2/month feature. Subscribe to use it."
            ], discord.Color.blurple(), buttons)
            await interaction.followup.send(view=card, ephemeral=True)
            return
        await func(self, interaction, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


class MediaConnectCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    connect = app_commands.Group(name="connect", description="Connect a media server or cloud folder you own")
    movie = app_commands.Group(name="movie", description="Search and play from your connected library")

    # ── Subscription ─────────────────────────────────────────────────

    @connect.command(name="subscribe", description="Subscribe to Media Connect ($2/month)")
    async def subscribe(self, interaction: discord.Interaction):
        await interaction.response.defer()
        active = await db.is_media_connect_active(interaction.user.id)
        if active:
            sub = await db.get_media_connect_subscription(interaction.user.id)
            await interaction.followup.send(
                f"✅ Already subscribed — active until {sub['expires_at'].strftime('%Y-%m-%d')}.", ephemeral=True
            )
            return

        result = paystack.initialize_payment(
            email=f"{interaction.user.id}@discord.user",  # Paystack requires an email; users pay via card/mobile money regardless
            amount_pesewas=MEDIA_CONNECT_FEE_GHS * 100,
            user_id=interaction.user.id,
            bot_name="Media Connect",
            payment_type="media_connect_subscription",
            extra_metadata={"provider": "discord"},
        )
        if not result or result.get("status") != "success":
            await interaction.followup.send("❌ Couldn't start checkout right now. Try again shortly.", ephemeral=True)
            return

        await interaction.followup.send(
            f"💳 **Media Connect — GHS {MEDIA_CONNECT_FEE_GHS}/month (~$2)**\n"
            f"[Complete payment]({result['authorization_url']})\n\n"
            f"Once paid, your access activates automatically within a minute — then run `/connect jellyfin`, "
            f"`/connect plex`, or `/connect gdrive`.",
            ephemeral=True,
        )

    @connect.command(name="status", description="Check your Media Connect subscription status")
    async def sub_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sub = await db.get_media_connect_subscription(interaction.user.id)
        active = await db.is_media_connect_active(interaction.user.id)
        if not sub or not active:
            buttons = [ActionButton("Subscribe — $2/mo", discord.ButtonStyle.success, self, "subscribe", emoji="💳")]
            card = NavCardView("Media Connect", ["No active subscription."], discord.Color.red(), buttons)
            await interaction.followup.send(view=card, ephemeral=True)
            return
        buttons = [refresh_button(self, "sub_status")]
        card = NavCardView("Media Connect", [f"Active until **{sub['expires_at'].strftime('%Y-%m-%d')}**."],
                            discord.Color.green(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    # ── Jellyfin ─────────────────────────────────────────────────────

    @connect.command(name="jellyfin", description="Connect your own Jellyfin server")
    @app_commands.describe(server_url="Your Jellyfin server URL", api_key="An API key from your server's dashboard")
    @_require_subscription
    async def connect_jellyfin(self, interaction: discord.Interaction, server_url: str, api_key: str):
        await interaction.response.defer(ephemeral=True)
        try:
            info = await jellyfin_client.verify_connection(server_url, api_key)
        except jellyfin_client.JellyfinError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        await db.set_jellyfin_connection(interaction.user.id, server_url, api_key, info["jellyfin_user_id"])
        buttons = [ActionButton("Search Library", discord.ButtonStyle.primary, self, "movie_search_prompt", emoji="🔍")]
        card = NavCardView("✅ Jellyfin connected", [
            f"Connected to **{info['server_name']}** (Jellyfin {info['version']}).",
            "Use `/movie search` to find something to watch.",
        ], discord.Color.green(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    async def movie_search_prompt(self, interaction: discord.Interaction):
        await interaction.response.send_message("Run `/movie search <title>` to look something up.", ephemeral=True)

    # ── Plex ─────────────────────────────────────────────────────────

    @connect.command(name="plex", description="Connect your own Plex server")
    @_require_subscription
    async def connect_plex(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            pin = await plex_client.create_pin()
        except plex_client.PlexError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        await db.set_plex_pin_session(interaction.user.id, pin["id"])
        auth_url = plex_client.build_auth_url(pin["code"])
        buttons = [ActionButton("I've approved it", discord.ButtonStyle.success, self, "plex_confirm", emoji="✅")]
        card = NavCardView("Connect Plex", [
            f"1. Click **[Sign in to Plex]({auth_url})** and approve access.",
            "2. Come back here and press the button below.",
            "-# This code expires in a few minutes.",
        ], discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    async def plex_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        pin_id = await db.get_plex_pin_session(interaction.user.id)
        if not pin_id:
            await interaction.followup.send("No pending Plex login. Run `/connect plex` again.", ephemeral=True)
            return

        try:
            token = await plex_client.check_pin(pin_id)
        except plex_client.PlexError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if not token:
            await interaction.followup.send(
                "Not approved yet — click the sign-in link first, then press this button again.", ephemeral=True
            )
            return

        try:
            servers = await plex_client.list_servers(token)
        except plex_client.PlexError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if not servers:
            await interaction.followup.send("No Plex Media Servers found on your account.", ephemeral=True)
            return

        await db.delete_plex_pin_session(interaction.user.id)

        if len(servers) == 1:
            s = servers[0]
            await db.set_plex_connection(interaction.user.id, s["access_token"], s["name"], s["base_url"])
            await interaction.followup.send(f"✅ Connected to **{s['name']}**. Use `/movie search` to find something.", ephemeral=True)
            return

        # Multiple servers — let them pick. A ChannelSelect-style dropdown
        # would need a custom discord.ui.Select subclass (same shape as
        # welcome.py's WelcomeThemeSelect) for >5 servers; most accounts
        # have 1-2 Plex servers, so a button per server (capped at 5,
        # matching the old NavView behavior) keeps this simple.
        buttons = [
            ActionButton(s["name"][:70], discord.ButtonStyle.primary, self, "plex_pick_server",
                         args=(s["name"], s["base_url"], s["access_token"]))
            for s in servers[:5]
        ]
        card = NavCardView("Pick which server to connect", [], discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    async def plex_pick_server(self, interaction: discord.Interaction, name: str, base_url: str, access_token: str):
        await interaction.response.defer(ephemeral=True)
        await db.set_plex_connection(interaction.user.id, access_token, name, base_url)
        await interaction.followup.send(f"✅ Connected to **{name}**. Use `/movie search` to find something.", ephemeral=True)

    # ── Google Drive ─────────────────────────────────────────────────

    @connect.command(name="gdrive", description="Connect a Google Drive folder you own")
    @_require_subscription
    async def connect_gdrive(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not gdrive_client.GOOGLE_CLIENT_ID:
            await interaction.followup.send(
                "Google Drive connection isn't configured on this bot yet.", ephemeral=True
            )
            return
        state = secrets_module.token_urlsafe(32)
        await db.create_gdrive_oauth_state(state, interaction.user.id)
        url = gdrive_client.build_authorize_url(state)
        await interaction.followup.send(
            "Click below to connect a Google Drive folder. You'll only be asked to grant access to files "
            "this bot creates/opens — not your whole Drive.\n\n"
            f"[Connect Google Drive]({url})\n\n_This link expires in a few minutes._",
            ephemeral=True,
        )

    # ── Disconnect ───────────────────────────────────────────────────

    @connect.command(name="disconnect", description="Disconnect a media source and forget its credentials")
    @app_commands.describe(source="Which connection to remove")
    @app_commands.choices(source=[
        app_commands.Choice(name="Jellyfin", value="jellyfin"),
        app_commands.Choice(name="Plex", value="plex"),
        app_commands.Choice(name="Google Drive", value="gdrive"),
    ])
    async def disconnect(self, interaction: discord.Interaction, source: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        if source.value == "jellyfin":
            await db.delete_jellyfin_connection(interaction.user.id)
        elif source.value == "plex":
            await db.delete_plex_connection(interaction.user.id)
        else:
            await db.delete_gdrive_connection(interaction.user.id)
        await interaction.followup.send(f"🗑️ Disconnected {source.name}. Your credentials were deleted.", ephemeral=True)

    # ── Search ───────────────────────────────────────────────────────

    @movie.command(name="search", description="Search your connected library")
    @app_commands.describe(title="What to search for")
    @_require_subscription
    async def search(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer(ephemeral=True)

        jf = await db.get_jellyfin_connection(interaction.user.id)
        gd = await db.get_gdrive_connection(interaction.user.id)
        px = await db.get_plex_connection(interaction.user.id)

        if not jf and not gd and not px:
            await interaction.followup.send(
                "You haven't connected a media source yet. Use `/connect jellyfin`, `/connect plex`, or `/connect gdrive` first.",
                ephemeral=True,
            )
            return

        results = []
        if jf:
            try:
                found = await jellyfin_client.search_movies(jf["server_url"], jf["api_key"], jf["jellyfin_user_id"], title)
                results.extend([{"source": "jellyfin", **f} for f in found])
            except jellyfin_client.JellyfinError as e:
                await interaction.followup.send(f"⚠️ Jellyfin search failed: {e}", ephemeral=True)

        if px:
            try:
                found = await plex_client.search_movies(px["base_url"], px["access_token"], title)
                results.extend([{"source": "plex", **f} for f in found])
            except plex_client.PlexError as e:
                await interaction.followup.send(f"⚠️ Plex search failed: {e}", ephemeral=True)

        if gd:
            try:
                token = await gdrive_client.ensure_valid_token(gd)
                found = await gdrive_client.search_files(token, gd["folder_id"], title)
                results.extend([{"source": "gdrive", "id": f["id"], "name": f["name"]} for f in found])
            except gdrive_client.GDriveError as e:
                await interaction.followup.send(f"⚠️ Google Drive search failed: {e}", ephemeral=True)

        if not results:
            await interaction.followup.send(f"No matches for **{title}** in your connected library.", ephemeral=True)
            return

        lines = []
        buttons = []
        for r in results[:5]:
            label = r["name"] + (f" ({r['year']})" if r.get("year") else "")
            lines.append(f"**{label}**\n-# Source: {r['source'].title()}")
            buttons.append(ActionButton(
                r["name"][:70], discord.ButtonStyle.success, self, "play_result", emoji="▶️",
                args=(r["source"], r["id"]),
            ))
        card = NavCardView(f'🔍 Results for "{title}"', lines, discord.Color.blurple(), buttons[:5])
        await interaction.followup.send(view=card, ephemeral=True)

    async def play_result(self, interaction: discord.Interaction, source: str, item_id: str):
        await self._send_play_link(interaction, source, item_id)

    @movie.command(name="play", description="Get a private playback link for something in your library")
    @app_commands.describe(item_id="The item id shown in /movie search or /movie library")
    @_require_subscription
    async def play(self, interaction: discord.Interaction, item_id: str):
        jf = await db.get_jellyfin_connection(interaction.user.id)
        px = await db.get_plex_connection(interaction.user.id)
        source = "jellyfin" if jf else ("plex" if px else "gdrive")
        await self._send_play_link(interaction, source, item_id)

    async def _send_play_link(self, interaction: discord.Interaction, source: str, item_id: str):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if source == "jellyfin":
            jf = await db.get_jellyfin_connection(interaction.user.id)
            if not jf:
                await interaction.followup.send("Your Jellyfin connection isn't set up anymore.", ephemeral=True)
                return
            url = jellyfin_client.build_stream_url(jf["server_url"], jf["api_key"], item_id)
        elif source == "plex":
            px = await db.get_plex_connection(interaction.user.id)
            if not px:
                await interaction.followup.send("Your Plex connection isn't set up anymore.", ephemeral=True)
                return
            try:
                url = await plex_client.build_download_url(px["base_url"], px["access_token"], item_id)
            except plex_client.PlexError as e:
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
                return
        else:
            gd = await db.get_gdrive_connection(interaction.user.id)
            if not gd:
                await interaction.followup.send("Your Google Drive connection isn't set up anymore.", ephemeral=True)
                return
            token = await gdrive_client.ensure_valid_token(gd)
            url = gdrive_client.build_stream_url(item_id, token)

        await interaction.followup.send(
            f"▶️ **Your playback link** (private to you, streams straight from your own storage):\n{url}",
            ephemeral=True,
        )

    @movie.command(name="library", description="List everything in your connected library")
    @_require_subscription
    async def library(self, interaction: discord.Interaction):
        jf = await db.get_jellyfin_connection(interaction.user.id)
        px = await db.get_plex_connection(interaction.user.id)
        if not jf and not px:
            await interaction.followup.send("Connect Jellyfin or Plex first with `/connect`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if jf:
                items = await jellyfin_client.list_library(jf["server_url"], jf["api_key"], jf["jellyfin_user_id"])
            else:
                items = await plex_client.list_library(px["base_url"], px["access_token"])
        except (jellyfin_client.JellyfinError, plex_client.PlexError) as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        if not items:
            await interaction.followup.send("Your library is empty.", ephemeral=True)
            return
        lines = ["\n".join(f"• {i['name']}" + (f" ({i['year']})" if i.get("year") else "") for i in items[:25])]
        buttons = [refresh_button(self, "library")]
        card = NavCardView("Your library", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaConnectCog(bot))
