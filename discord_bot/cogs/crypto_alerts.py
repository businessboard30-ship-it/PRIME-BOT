"""
Crypto price alerts — new Discord surface for logic that already existed
platform-agnostically in modules/superbot_adapter.py (set_alert/
get_user_alerts/remove_alert/clear_alerts) but had no Telegram OR Discord
UI wired to it yet. Pairs with modules/external_apis.get_crypto_price
(same one /crypto in external_tools.py uses) to actually check prices.

DELIVERY DESIGN — read before enabling on more than one process:
Alerts are user-scoped with no clone_id/guild column (superbot_crypto_alerts
has none), so if this loop ran on every clone process each active alert
would fire once per running process — the same user could get duplicate
DMs. The loop below only starts when self.bot.clone_id is None (main bot
only), same convention clone_admin.py uses for "only makes sense once."
If you want alerts on clones too, that table needs a clone_id column first.

Delivery is a DM (fire-and-forget on triggered alerts, then remove_alert
deactivates it — one-shot, not recurring, matching the old set_alert/
remove_alert pair's shape). If the user has DMs closed from this bot,
the alert is still consumed (removed) rather than retried forever, and a
warning is logged.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import get_pool
from modules.superbot_adapter import set_alert, get_user_alerts, remove_alert, clear_alerts
from modules.external_apis import get_crypto_price
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MINUTES = 5


async def _all_active_alerts() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM superbot_crypto_alerts WHERE active = TRUE")
    return [dict(r) for r in rows]


class CryptoAlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if getattr(bot, "clone_id", None) is None:
            self._check_alerts.start()

    def cog_unload(self):
        self._check_alerts.cancel()

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def _check_alerts(self):
        try:
            alerts = await _all_active_alerts()
        except Exception as e:
            logger.error(f"[crypto_alerts] Could not load alerts: {e}")
            return

        # Group by coin so a popular coin is only priced once per sweep.
        by_coin: dict[str, list] = {}
        for alert in alerts:
            by_coin.setdefault(alert["coin"], []).append(alert)

        for coin, coin_alerts in by_coin.items():
            price_data = await get_crypto_price(coin)
            if not price_data:
                continue
            price = price_data.get("price_usd", 0) or 0

            for alert in coin_alerts:
                threshold = alert["price_threshold"]
                triggered = (
                    (alert["alert_type"] == "above" and price >= threshold)
                    or (alert["alert_type"] == "below" and price <= threshold)
                )
                if not triggered:
                    continue

                await remove_alert(alert["user_id"], coin)
                try:
                    user = await self.bot.fetch_user(alert["user_id"])
                    embed = discord.Embed(
                        title="🔔 Crypto Alert Triggered",
                        description=(
                            f"**{coin.title()}** is now **${price:,.2f}** "
                            f"({alert['alert_type']} your ${threshold:,.2f} target)."
                        ),
                        color=discord.Color.gold(),
                    )
                    await user.send(embed=embed)
                except discord.Forbidden:
                    logger.warning(f"[crypto_alerts] Can't DM user {alert['user_id']} — alert consumed anyway.")
                except Exception as e:
                    logger.error(f"[crypto_alerts] Failed to notify user {alert['user_id']}: {e}")

    @_check_alerts.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    alert = app_commands.Group(name="alert", description="Crypto price alerts")

    @alert.command(name="set", description="Get notified when a coin crosses a price")
    @app_commands.describe(coin="Coin id, e.g. bitcoin", price="Target price in USD", direction="above or below")
    @app_commands.choices(direction=[
        app_commands.Choice(name="above", value="above"),
        app_commands.Choice(name="below", value="below"),
    ])
    async def alert_set(self, interaction: discord.Interaction, coin: str, price: float, direction: app_commands.Choice[str]):
        coin = coin.strip().lower()
        if price <= 0:
            await interaction.response.send_message("Price must be positive.", ephemeral=True)
            return
        ok = await set_alert(interaction.user.id, coin, price, direction.value)
        if not ok:
            await interaction.response.send_message("❌ Couldn't create that alert. Try again.", ephemeral=True)
            return
        buttons = [ActionButton("My alerts", discord.ButtonStyle.secondary, self, "alert_list", emoji="🔔")]
        card = NavCardView("🔔 Alert set", [f"I'll DM you when **{coin}** goes {direction.value} **${price:,.2f}**."],
                            discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @alert.command(name="list", description="Show your active price alerts")
    async def alert_list(self, interaction: discord.Interaction):
        alerts = await get_user_alerts(interaction.user.id)
        if not alerts:
            await interaction.response.send_message("You have no active alerts. Use `/alert set` to create one.", ephemeral=True)
            return
        lines = [f"• **{a['coin']}** {a['alert_type']} ${a['price_threshold']:,.2f}" for a in alerts]
        buttons = [
            refresh_button(self, "alert_list"),
            ActionButton("Clear all", discord.ButtonStyle.danger, self, "alert_clear", emoji="🗑️"),
        ]
        card = NavCardView("🔔 Your crypto alerts", lines, discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @alert.command(name="remove", description="Remove your alert for a specific coin")
    @app_commands.describe(coin="Coin id to stop tracking")
    async def alert_remove(self, interaction: discord.Interaction, coin: str):
        await remove_alert(interaction.user.id, coin.strip().lower())
        await interaction.response.send_message(f"Removed alert(s) for **{coin}**.", ephemeral=True)

    @alert.command(name="clear", description="Remove all your alerts")
    async def alert_clear(self, interaction: discord.Interaction):
        await clear_alerts(interaction.user.id)
        await interaction.response.send_message("Cleared all your alerts.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CryptoAlertsCog(bot))
