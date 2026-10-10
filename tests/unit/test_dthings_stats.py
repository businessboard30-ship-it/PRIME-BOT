import asyncio

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import config
from discord_bot.cogs import dthings_stats
from discord_bot.cogs.dthings_stats import DthingsStatsCog, build_commands_payload, build_stats_payload


def test_stats_payload():
    assert build_stats_payload(146, None) == {"server_count": 146, "shard_count": 1}
    assert build_stats_payload(10, 2, 500) == {"server_count": 10, "shard_count": 2, "user_count": 500}


def test_commands_payload_hides_admin():
    raw = [{"name": "help"}, {"name": "Admin"}]
    assert build_commands_payload(raw, {"admin"}) == [{"name": "help"}]


async def _cb(interaction):
    pass


def test_cog_posts_to_dthings(monkeypatch):
    seen = []

    async def handler(request):
        seen.append((request.method, request.path, request.headers.get("Authorization"), await request.json()))
        return web.json_response({"success": True})

    async def run():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(dthings_stats, "DTHINGS_API", f"http://127.0.0.1:{port}/api")
        monkeypatch.setattr(config, "DTHINGS_API_KEY", "tok123")
        monkeypatch.setattr(config, "DTHINGS_BOT_ID", "555")
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        bot.tree.add_command(app_commands.Command(name="ping", description="Pong", callback=_cb))
        bot.tree.add_command(app_commands.Command(name="admin", description="x", callback=_cb))
        monkeypatch.setattr(commands.Bot, "guilds", property(lambda self: [type("G", (), {"member_count": 3})()] * 7))
        monkeypatch.setattr(commands.Bot, "application_id", property(lambda self: 555))
        cog = DthingsStatsCog(bot)
        assert cog.enabled and cog._is_listed_bot()
        cog._stats_loop.cancel()
        assert await cog.post_stats()
        assert await cog.push_commands()
        await runner.cleanup()

    asyncio.run(run())
    stats, cmds = seen
    assert stats[:3] == ("POST", "/api/bots/555/stats", "tok123")
    assert stats[3]["server_count"] == 7 and stats[3]["user_count"] == 21
    assert cmds[1] == "/api/bots/555/commands"
    assert [c["name"] for c in cmds[3]["commands"]] == ["ping"]
