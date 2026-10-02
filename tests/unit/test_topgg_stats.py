import asyncio
import types

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import config
from discord_bot.cogs import topgg_stats
from discord_bot.cogs.topgg_stats import TopggStatsCog, build_commands_payload, build_metrics_payload


def test_metrics_payload_defaults_shard_to_one():
    assert build_metrics_payload(146, None) == {"server_count": 146, "shard_count": 1}
    assert build_metrics_payload(3000, 4) == {"server_count": 3000, "shard_count": 4}


def test_commands_payload_hides_admin_case_insensitive():
    raw = [{"name": "help"}, {"name": "Admin"}, {"name": "rank"}]
    assert [c["name"] for c in build_commands_payload(raw, {"admin"})] == ["help", "rank"]
    assert build_commands_payload([{"name": "admin"}], {"admin"}) == []


def test_cog_posts_metrics_and_commands_to_topgg(monkeypatch):
    seen = []

    async def handler(request):
        seen.append((request.method, request.path, request.headers.get("Authorization"),
                     await request.json()))
        return web.Response(status=204)

    async def run():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(topgg_stats, "TOPGG_API", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(config, "TOPGG_TOKEN", "tok123")

        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        bot.clone_id = None
        bot.tree.add_command(app_commands.Command(name="ping", description="Pong",
                                                  callback=_cb))
        bot.tree.add_command(app_commands.Command(name="admin", description="[Owner] x",
                                                  callback=_cb))
        monkeypatch.setattr(commands.Bot, "guilds", property(lambda self: [object()] * 7))
        monkeypatch.setattr(config, "TOPGG_BOT_ID", "555")
        monkeypatch.setattr(commands.Bot, "application_id", property(lambda self: 555))
        cog = TopggStatsCog(bot)
        assert cog.enabled and cog._is_listed_bot()
        cog._stats_loop.cancel()  # we drive it by hand; the loop waits for gateway-ready
        assert await cog.post_metrics()
        assert await cog.push_commands()
        await runner.cleanup()

    asyncio.run(run())
    (m_method, m_path, m_auth, m_body), (c_method, c_path, _, c_body) = seen
    assert (m_method, m_path, m_auth) == ("PATCH", "/projects/@me/metrics", "Bearer tok123")
    assert m_body == {"server_count": 7, "shard_count": 1}
    assert (c_method, c_path) == ("PUT", "/projects/@me/commands")
    assert [c["name"] for c in c_body] == ["ping"]  # admin hidden


def test_only_the_listed_bot_posts_and_missing_token_is_disabled(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        monkeypatch.setattr(config, "TOPGG_TOKEN", "tok")
        monkeypatch.setattr(config, "TOPGG_BOT_ID", "555")
        # main bot with a different application id must NOT post
        bot.clone_id = None
        monkeypatch.setattr(commands.Bot, "application_id", property(lambda self: 111))
        cog = TopggStatsCog(bot)
        cog._stats_loop.cancel()
        assert not cog._is_listed_bot()
        # a clone whose application id matches the listing IS the poster
        bot.clone_id = 8
        monkeypatch.setattr(commands.Bot, "application_id", property(lambda self: 555))
        cog = TopggStatsCog(bot)
        cog._stats_loop.cancel()
        assert cog._is_listed_bot()
        monkeypatch.setattr(config, "TOPGG_TOKEN", "")
        assert not TopggStatsCog(bot).enabled          # no token -> no-op
    asyncio.run(run())


async def _cb(interaction: discord.Interaction):
    pass
