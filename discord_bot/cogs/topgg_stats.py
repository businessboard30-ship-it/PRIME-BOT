# path: discord_bot/cogs/topgg_stats.py

"""
Keeps the Top.gg listing in sync with the bot:

  - server_count / shard_count, posted on start and then every
    TOPGG_POST_INTERVAL_MINUTES (PATCH /projects/@me/metrics)
  - the slash-command list shown on the listing's "Commands" tab, pushed once
    per start (PUT /projects/@me/commands), minus anything in
    TOPGG_HIDE_COMMANDS (default: the owner-only /admin group)

Only the MAIN bot does this. Clones are separate bot accounts with their own
guild counts and are not what the Top.gg listing represents, so a clone
(clone_id is not None) never posts — otherwise the listing's server count would
be wrong. Needs TOPGG_TOKEN (the project token from the Top.gg dashboard) in
the main bot's environment; with no token the cog loads and does nothing.

Every request is best-effort: failures are logged and retried on the next
tick, never raised.
"""

import logging

import aiohttp
from discord.ext import commands, tasks

import config

logger = logging.getLogger(__name__)

TOPGG_API = "https://top.gg/api/v1"
REQUEST_TIMEOUT_SECONDS = 10


def build_metrics_payload(guild_count: int, shard_count) -> dict:
    return {"server_count": int(guild_count), "shard_count": int(shard_count or 1)}


def build_commands_payload(command_dicts: list, hidden: set) -> list:
    """Drops hidden top-level commands (case-insensitive) from Discord's
    application-command JSON."""
    return [c for c in command_dicts if str(c.get("name", "")).lower() not in hidden]


class TopggStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._commands_pushed = False
        self.enabled = bool(config.TOPGG_TOKEN) and getattr(bot, "clone_id", None) is None
        if self.enabled:
            self._stats_loop.change_interval(minutes=config.TOPGG_POST_INTERVAL_MINUTES)
            self._stats_loop.start()
        elif getattr(bot, "clone_id", None) is None:
            logger.info("[topgg] TOPGG_TOKEN not set — server count / command sync disabled")

    def cog_unload(self):
        self._stats_loop.cancel()

    async def _request(self, method: str, path: str, body):
        """Returns (status, text). Never raises."""
        headers = {"Authorization": f"Bearer {config.TOPGG_TOKEN}"}
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.request(method, f"{TOPGG_API}{path}", json=body) as r:
                    text = "" if r.status == 204 else (await r.text())[:300]
                    if r.status == 429:
                        logger.warning("[topgg] rate limited (retry-after=%s)", r.headers.get("Retry-After"))
                    return r.status, text
        except Exception as e:
            logger.warning("[topgg] request %s %s failed: %s", method, path, e)
            return 0, str(e)

    async def post_metrics(self) -> bool:
        payload = build_metrics_payload(len(self.bot.guilds), self.bot.shard_count)
        status, text = await self._request("PATCH", "/projects/@me/metrics", payload)
        if 200 <= status < 300:
            logger.info("[topgg] posted metrics %s", payload)
            return True
        logger.warning("[topgg] metrics post failed: HTTP %s %s", status, text)
        return False

    async def push_commands(self) -> bool:
        raw = [c.to_dict(self.bot.tree) for c in self.bot.tree.get_commands()]
        payload = build_commands_payload(raw, config.TOPGG_HIDE_COMMANDS)
        if not payload:  # an empty array would CLEAR the listing's commands
            return False
        status, text = await self._request("PUT", "/projects/@me/commands", payload)
        if 200 <= status < 300:
            logger.info("[topgg] pushed %d commands (hid %d)", len(payload), len(raw) - len(payload))
            return True
        logger.warning("[topgg] command push failed: HTTP %s %s", status, text)
        return False

    @tasks.loop(minutes=30)
    async def _stats_loop(self):
        try:
            await self.post_metrics()
            if not self._commands_pushed:
                self._commands_pushed = await self.push_commands()
        except Exception:
            logger.exception("[topgg] stats loop iteration failed")

    @_stats_loop.before_loop
    async def _before_stats_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TopggStatsCog(bot))
