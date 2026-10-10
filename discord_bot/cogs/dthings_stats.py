# path: discord_bot/cogs/dthings_stats.py

"""
Keeps the DiscordThings (dsc.sh) listing in sync with the bot:

  - server_count / shard_count / user_count, posted on start, on guild
    join/leave (debounced), then every DTHINGS_POST_INTERVAL_MINUTES
    (POST /bots/{id}/stats)
  - the slash-command list for the listing's public page, pushed once per
    process start (POST /bots/{id}/commands), minus anything in
    DTHINGS_HIDE_COMMANDS (default: the owner-only /admin group)

Only the process whose Discord application id equals DTHINGS_BOT_ID posts, so
no other bot/clone can overwrite the listing's numbers. Auth is the raw API
token in the Authorization header. The token comes from the DTHINGS_API_KEY
environment variable (set it in Railway) — never commit it. With no token the
cog loads and does nothing.

Rate limits (per dsc.sh docs): 1 request/minute per endpoint, 30/min per token.
Every request is best-effort: failures are logged and retried on the next tick.
"""

import asyncio
import logging

import aiohttp
from discord.ext import commands, tasks

import config

logger = logging.getLogger(__name__)

DTHINGS_API = "https://dsc.sh/api"
REQUEST_TIMEOUT_SECONDS = 10
# dsc.sh allows 1 request/minute per endpoint; guild join/leave bursts are
# collapsed into one post this many seconds after the first event.
EVENT_POST_DEBOUNCE_SECONDS = 90


def build_stats_payload(guild_count: int, shard_count, user_count=None) -> dict:
    payload = {"server_count": int(guild_count), "shard_count": int(shard_count or 1)}
    if user_count is not None:
        payload["user_count"] = int(user_count)
    return payload


def build_commands_payload(command_dicts: list, hidden: set) -> list:
    """Drops hidden top-level commands (case-insensitive)."""
    return [c for c in command_dicts if str(c.get("name", "")).lower() not in hidden]


class DthingsStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._commands_pushed = False
        self._skip_logged = False
        self._event_post_task = None
        self.enabled = bool(config.DTHINGS_API_KEY)
        if self.enabled:
            self._stats_loop.change_interval(minutes=config.DTHINGS_POST_INTERVAL_MINUTES)
            self._stats_loop.start()
        else:
            logger.info("[dthings] DTHINGS_API_KEY not set — server count / command sync disabled")

    def _is_listed_bot(self) -> bool:
        listed = str(config.DTHINGS_BOT_ID or "").strip()
        me = getattr(self.bot, "application_id", None) or getattr(getattr(self.bot, "user", None), "id", None)
        return bool(listed) and me is not None and str(me) == listed

    def cog_unload(self):
        self._stats_loop.cancel()
        if self._event_post_task and not self._event_post_task.done():
            self._event_post_task.cancel()

    async def _post(self, path: str, body: dict):
        """Returns (status, text). Never raises."""
        url = f"{DTHINGS_API}/bots/{config.DTHINGS_BOT_ID}{path}"
        headers = {"Authorization": config.DTHINGS_API_KEY, "Content-Type": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(url, json=body) as r:
                    text = (await r.text())[:300]
                    if r.status == 429:
                        logger.warning("[dthings] rate limited (retry-after=%s)", r.headers.get("Retry-After"))
                    return r.status, text
        except Exception as e:
            logger.warning("[dthings] request POST %s failed: %s", path, e)
            return 0, str(e)

    async def post_stats(self) -> bool:
        guilds = self.bot.guilds
        users = sum(getattr(g, "member_count", 0) or 0 for g in guilds)
        payload = build_stats_payload(len(guilds), self.bot.shard_count, users)
        status, text = await self._post("/stats", payload)
        if 200 <= status < 300:
            logger.info("[dthings] posted stats %s", payload)
            return True
        logger.warning("[dthings] stats post failed: HTTP %s %s", status, text)
        return False

    async def push_commands(self) -> bool:
        raw = [c.to_dict(self.bot.tree) for c in self.bot.tree.get_commands()]
        payload = build_commands_payload(raw, config.DTHINGS_HIDE_COMMANDS)
        if not payload:
            return False
        status, text = await self._post("/commands", {"commands": payload})
        if 200 <= status < 300:
            logger.info("[dthings] pushed %d commands (hid %d)", len(payload), len(raw) - len(payload))
            return True
        logger.warning("[dthings] command push failed: HTTP %s %s", status, text)
        return False

    async def _debounced_post(self):
        try:
            await asyncio.sleep(EVENT_POST_DEBOUNCE_SECONDS)
            await self.post_stats()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[dthings] event-driven stats post failed")

    def _schedule_event_post(self):
        if not self.enabled or not self.bot.is_ready() or not self._is_listed_bot():
            return
        if self._event_post_task is None or self._event_post_task.done():
            self._event_post_task = asyncio.create_task(self._debounced_post())

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self._schedule_event_post()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._schedule_event_post()

    @tasks.loop(minutes=30)
    async def _stats_loop(self):
        try:
            if not self._is_listed_bot():
                if not self._skip_logged:
                    self._skip_logged = True
                    logger.info("[dthings] this bot (%s) is not the listed bot (DTHINGS_BOT_ID=%s) — not posting",
                                getattr(self.bot.user, "id", "?"), config.DTHINGS_BOT_ID)
                return
            await self.post_stats()
            if not self._commands_pushed:
                # Separate request, so wait out the per-endpoint 1/min limit isn't needed;
                # they're different endpoints.
                self._commands_pushed = await self.push_commands()
        except Exception:
            logger.exception("[dthings] stats loop iteration failed")

    @_stats_loop.before_loop
    async def _before_stats_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DthingsStatsCog(bot))
