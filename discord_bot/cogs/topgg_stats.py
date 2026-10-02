# path: discord_bot/cogs/topgg_stats.py

"""
Keeps the Top.gg listing in sync with the bot:

  - server_count / shard_count, posted on start, on every guild join/leave
    (debounced), and then every TOPGG_POST_INTERVAL_MINUTES as a safety net
    (PATCH /projects/@me/metrics)
  - the slash-command list shown on the listing's "Commands" tab, pushed once
    per start (PUT /projects/@me/commands), minus anything in
    TOPGG_HIDE_COMMANDS (default: the owner-only /admin group)
  - a one-time startup check (GET /projects/@me) that logs which Top.gg
    project the token actually belongs to, so a wrong/foreign token shows up in
    the logs instead of silently producing a wrong server count

Only the bot that IS the Top.gg listing posts: the process whose Discord
application id equals TOPGG_BOT_ID. That may be the main bot or a clone —
whichever account the listing was created for. Every other process (main bot or
clones) stays silent, otherwise it would overwrite the listing's server count
with its own guild count. Needs TOPGG_TOKEN (the project token from the Top.gg
dashboard) and TOPGG_BOT_ID in that bot's environment; with no token the cog
loads and does nothing.

Every request is best-effort: failures are logged and retried on the next
tick, never raised.
"""

import asyncio
import json
import logging

import aiohttp
from discord.ext import commands, tasks

import config

logger = logging.getLogger(__name__)

TOPGG_API = "https://top.gg/api/v1"
REQUEST_TIMEOUT_SECONDS = 10
# Top.gg applies stricter rate limits to bot endpoints, so guild join/leave
# bursts (raids, mass kicks) are collapsed into one post this many seconds
# after the first event.
EVENT_POST_DEBOUNCE_SECONDS = 60


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
        self._token_checked = False
        self._skip_logged = False
        self._event_post_task = None
        # Whether this process is the listed bot can only be known once the
        # gateway is ready (bot.user / application_id), so the loop always
        # starts and _is_listed_bot() gates every post.
        self.enabled = bool(config.TOPGG_TOKEN)
        if self.enabled:
            self._stats_loop.change_interval(minutes=config.TOPGG_POST_INTERVAL_MINUTES)
            self._stats_loop.start()
        else:
            logger.info("[topgg] TOPGG_TOKEN not set — server count / command sync disabled")

    def _is_listed_bot(self) -> bool:
        listed = str(config.TOPGG_BOT_ID or "").strip()
        me = getattr(self.bot, "application_id", None) or getattr(getattr(self.bot, "user", None), "id", None)
        return bool(listed) and me is not None and str(me) == listed

    def cog_unload(self):
        self._stats_loop.cancel()
        if self._event_post_task and not self._event_post_task.done():
            self._event_post_task.cancel()

    async def _request(self, method: str, path: str, body=None):
        """Returns (status, text). Never raises."""
        headers = {"Authorization": f"Bearer {config.TOPGG_TOKEN}"}
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                kwargs = {"json": body} if body is not None else {}
                async with session.request(method, f"{TOPGG_API}{path}", **kwargs) as r:
                    text = "" if r.status == 204 else (await r.text())[:300]
                    if r.status == 429:
                        logger.warning("[topgg] rate limited (retry-after=%s)", r.headers.get("Retry-After"))
                    return r.status, text
        except Exception as e:
            logger.warning("[topgg] request %s %s failed: %s", method, path, e)
            return 0, str(e)

    async def verify_token(self) -> bool:
        """Logs which Top.gg project the token belongs to. A token from a
        different project (or a stale one) is the classic cause of a listing
        count that never matches this bot."""
        status, text = await self._request("GET", "/projects/@me")
        if 200 <= status < 300:
            try:
                info = json.loads(text) if text else {}
            except Exception:
                info = {}
            logger.info("[topgg] token OK — project %r (id=%s, type=%s, platform=%s); this bot (%s) is in %d guild(s)",
                        info.get("name"), info.get("id"), info.get("type"), info.get("platform"),
                        getattr(self.bot.user, "id", "?"), len(self.bot.guilds))
            return True
        logger.error("[topgg] token check failed: HTTP %s %s — metrics posts will be rejected; "
                     "regenerate the project token in the Top.gg dashboard (Integrations & API)", status, text)
        return False

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

    # ── guild join/leave: keep the count fresh between interval ticks ──

    async def _debounced_post(self):
        try:
            await asyncio.sleep(EVENT_POST_DEBOUNCE_SECONDS)
            await self.post_metrics()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[topgg] event-driven metrics post failed")

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
                    logger.info("[topgg] this bot (%s, clone_id=%s) is not the listed bot (TOPGG_BOT_ID=%s) — "
                                "not posting, so it cannot overwrite the listing's server count",
                                getattr(self.bot.user, "id", "?"), getattr(self.bot, "clone_id", None),
                                config.TOPGG_BOT_ID)
                return
            if not self._token_checked:
                self._token_checked = True
                await self.verify_token()
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
