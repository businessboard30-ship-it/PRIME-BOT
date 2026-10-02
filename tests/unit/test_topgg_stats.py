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
        cog = TopggStatsCog(bot)
        assert cog.enabled
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


def test_clone_and_missing_token_are_disabled(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        monkeypatch.setattr(config, "TOPGG_TOKEN", "tok")
        bot.clone_id = 8
        assert not TopggStatsCog(bot).enabled          # clones never post
        bot.clone_id = None
        monkeypatch.setattr(config, "TOPGG_TOKEN", "")
        assert not TopggStatsCog(bot).enabled          # no token -> no-op
    asyncio.run(run())


async def _cb(interaction: discord.Interaction):
    pass
