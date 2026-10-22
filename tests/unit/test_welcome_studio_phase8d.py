"""Welcome Card Studio Phase 8d: a raid of joins draws only a few cards at a time."""
import asyncio
from pathlib import Path

from discord_bot.cogs import welcome as wl

ROOT = Path(__file__).resolve().parents[2]


def test_at_most_the_limit_run_at_once_and_none_is_dropped():
    async def main():
        running = peak = done = 0

        async def job():
            nonlocal running, peak, done
            async with wl._join_render_slots():
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1
                done += 1

        await asyncio.gather(*(job() for _ in range(30)))
        return peak, done

    peak, done = asyncio.run(main())
    assert peak <= wl._JOIN_RENDER_LIMIT and done == 30


def test_join_render_goes_through_the_slots():
    src = (ROOT / "discord_bot/cogs/welcome.py").read_text()
    join = src.split("async def on_member_join")[1].split("\n    async def ")[0]
    assert "async with _join_render_slots():" in join and "to_thread(" in join
