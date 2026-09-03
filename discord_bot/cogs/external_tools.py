# path: discord_bot/cogs/external_tools.py

"""
External integrations — Discord equivalent of handlers/external_handler.py.

Reuses modules/external_apis.py (news, currency, stock, crypto, yt-dlp
download). The download path's old Telegram-derived 50MB size constant has
been fixed to match Discord's real cap (MAX_DOWNLOAD_FILE_SIZE in
external_apis.py, kept in sync with _DISCORD_SAFE_UPLOAD_BYTES below) and
its yt-dlp calls now run off the event loop — see external_apis.py for
details.

DELIBERATELY NOT PORTED: handlers/utility_paywall.py's paid gate on
Download (see ai_tools.py's docstring — same reasoning, same open product
question about how/whether Discord-side monetization should cover this).
Download is otherwise unrestricted here; add a check before the yt_dlp
call once that's decided.
"""

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from modules.external_apis import fetch_news, convert_currency, get_stock_chart, download_media, get_crypto_price
from discord_bot.cogs._views_shared import NavCardView, refresh_button
from discord_bot.cogs.media_storage import _clone_id_of

logger = logging.getLogger(__name__)

# Discord's own attachment size cap varies by server boost level (historically
# 8-10MB on a free/unboosted server, higher when boosted). 8MB is the safe
# floor — a boosted server can raise this, but we can't tell a guild's boost
# tier from here reliably enough to risk an upload failure after the download
# already succeeded, so we stay conservative and link out instead when unsure.
_DISCORD_SAFE_UPLOAD_BYTES = 8 * 1024 * 1024

DRM_BLOCKED_DOMAINS = {
    "spotify.com", "www.spotify.com", "open.spotify.com",
    "music.apple.com", "netflix.com", "www.netflix.com",
}


class ExternalToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="news", description="Get top headlines about a topic")
    @app_commands.describe(topic="Topic to search news for")
    async def news(self, interaction: discord.Interaction, topic: str):
        topic = topic.strip()[:100]
        if not topic:
            await interaction.response.send_message("Enter a topic to search for.", ephemeral=True)
            return
        await interaction.response.defer()
        articles = await fetch_news(topic, max_results=5)
        if not articles:
            await interaction.followup.send(f"📰 No news found for '{topic}'.")
            return

        lines = []
        for i, article in enumerate(articles, 1):
            title = article.get("title", "No title")
            title = title[:80] + "..." if len(title) > 80 else title
            source = article.get("source", "")
            suffix = f" — {source}" if source else ""
            lines.append(f"**{i}. {title}**\n[Read more]({article.get('url', '#')}){suffix}")
        buttons = [refresh_button(self, "news", args=(topic,))]
        card = NavCardView(f"📰 Top news: {topic}", lines, discord.Color.dark_teal(), buttons)
        await interaction.followup.send(view=card)

    @app_commands.command(name="convert", description="Convert an amount between currencies")
    @app_commands.describe(amount="Amount to convert", from_currency="e.g. USD", to_currency="e.g. GHS")
    async def convert(self, interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
        if amount <= 0:
            await interaction.response.send_message("Amount must be positive.", ephemeral=True)
            return
        await interaction.response.defer()
        result = await convert_currency(amount, from_currency, to_currency)
        if not result:
            await interaction.followup.send(f"❌ Couldn't convert {from_currency.upper()} to {to_currency.upper()}. Check the currency codes.")
            return
        embed = discord.Embed(title="💱 Currency Conversion", color=discord.Color.green())
        embed.description = (
            f"**{result['original_amount']} {result['from_currency']}** = "
            f"**{result['converted_amount']} {result['to_currency']}**\n\n"
            f"Rate: 1 {result['from_currency']} = {result['rate']:.4f} {result['to_currency']}"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="stock", description="Get a stock's current price and recent change")
    @app_commands.describe(ticker="Stock ticker, e.g. AAPL")
    async def stock(self, interaction: discord.Interaction, ticker: str):
        ticker = ticker.strip().upper()[:10]
        if not ticker:
            await interaction.response.send_message("Enter a ticker symbol.", ephemeral=True)
            return
        await interaction.response.defer()
        data = await get_stock_chart(ticker)
        if not data:
            await interaction.followup.send(f"❌ Couldn't find data for '{ticker}'. Check the ticker symbol.")
            return
        change = data["change_24h_percent"]
        arrow = "📈" if change >= 0 else "📉"
        line = (
            f"**${data['current_price']}**\n{change:+.2f}% (24h)\n"
            f"-# Period: {data['period']}"
        )
        buttons = [refresh_button(self, "stock", args=(ticker,))]
        card = NavCardView(f"{arrow} {data['ticker']}", [line],
                            discord.Color.green() if change >= 0 else discord.Color.red(), buttons)
        await interaction.followup.send(view=card)

    @app_commands.command(name="crypto", description="Get a cryptocurrency's current price")
    @app_commands.describe(coin="Coin id, e.g. bitcoin, ethereum")
    async def crypto(self, interaction: discord.Interaction, coin: str):
        coin = coin.strip().lower()
        if not coin:
            await interaction.response.send_message("Enter a coin, e.g. bitcoin.", ephemeral=True)
            return
        await interaction.response.defer()
        data = await get_crypto_price(coin)
        if not data:
            await interaction.followup.send(f"❌ Couldn't find price data for '{coin}'.")
            return
        change = data.get("change_24h_percent", 0) or 0
        arrow = "📈" if change >= 0 else "📉"
        line = f"**${data.get('price_usd', 0):,.2f}**\n{change:+.2f}% (24h)"
        if data.get("market_cap_usd"):
            line += f"\n-# Market cap: ${data['market_cap_usd']:,.0f}"
        buttons = [refresh_button(self, "crypto", args=(coin,))]
        card = NavCardView(f"{arrow} {data.get('coin', coin).title()}", [line],
                            discord.Color.green() if change >= 0 else discord.Color.red(), buttons)
        await interaction.followup.send(view=card)

    @app_commands.command(name="download", description="Download audio or video from a supported link (YouTube, etc.)")
    @app_commands.describe(url="Link to the media", media_type="audio or video")
    @app_commands.choices(media_type=[
        app_commands.Choice(name="audio", value="audio"),
        app_commands.Choice(name="video", value="video"),
    ])
    async def download(self, interaction: discord.Interaction, url: str, media_type: app_commands.Choice[str] = None):
        media_type_value = media_type.value if media_type else "audio"
        from urllib.parse import urlparse
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            domain = ""
        if domain in DRM_BLOCKED_DOMAINS:
            await interaction.response.send_message(
                "❌ That site is DRM-protected — downloading from it isn't possible.", ephemeral=True
            )
            return

        await interaction.response.defer()
        result = await download_media(url, media_type_value)
        if not result:
            await interaction.followup.send("❌ Download failed. Check the link and try again.")
            return
        if "error" in result:
            await interaction.followup.send(f"❌ {result['error']}")
            return

        filepath = result.get("filepath")
        try:
            size_bytes = os.path.getsize(filepath) if filepath else 0
            caption = f"**{result.get('title', 'Media')}** — {result.get('uploader', 'Unknown')} ({result.get('size_mb', 0)}MB)"

            # Route to the guild's configured media storage channel instead
            # of posting the file in the invoking channel — see
            # media_storage.py. Falls back to the old in-channel post if no
            # storage channel has been set up yet (owner hasn't run
            # /set-storage-channel), so downloads never silently vanish.
            storage_channel = None
            if interaction.guild is not None:
                config = await db.get_media_storage_config(interaction.guild.id, _clone_id_of(self.bot))
                storage_channel_id = config.get("storage_channel_id")
                if storage_channel_id:
                    storage_channel = interaction.guild.get_channel(storage_channel_id)

            if not filepath or size_bytes > _DISCORD_SAFE_UPLOAD_BYTES:
                await interaction.followup.send(
                    f"{caption}\n\n⚠️ File is too large to upload directly to Discord "
                    f"(over {_DISCORD_SAFE_UPLOAD_BYTES // (1024*1024)}MB on this server's boost tier)."
                )
                return

            if storage_channel is not None:
                await storage_channel.send(
                    content=f"{caption}\n-# Requested by {interaction.user.mention} in {interaction.channel.mention}",
                    file=discord.File(filepath, filename=result["filename"]),
                )
                await interaction.followup.send(f"✅ Saved to {storage_channel.mention}.")
            else:
                await interaction.followup.send(content=caption, file=discord.File(filepath, filename=result["filename"]))
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)


async def setup(bot: commands.Bot):
    await bot.add_cog(ExternalToolsCog(bot))
