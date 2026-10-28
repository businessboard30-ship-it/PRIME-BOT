"""The dashboard's single-flight lookups must be keyed per bot: a clone must never receive the main bot's answer."""
import asyncio

from api import dash


def test_once_does_not_share_a_lookup_between_main_bot_and_a_clone():
    async def run():
        gate = asyncio.Event()

        def factory_for(label):
            async def factory():
                await gate.wait()
                return label
            return factory

        async def as_bot(cid, label):
            dash._BOT.set((cid, "tok" if cid else None))
            return await dash._once("botguilds", factory_for(label))

        t_main = asyncio.ensure_future(as_bot(None, "main"))
        t_clone = asyncio.ensure_future(as_bot(7, "clone7"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        return await t_main, await t_clone

    assert asyncio.run(run()) == ("main", "clone7")


def test_once_still_shares_a_lookup_for_the_same_bot():
    async def run():
        gate = asyncio.Event()
        calls = []

        async def factory():
            calls.append(1)
            await gate.wait()
            return "x"

        async def call():
            dash._BOT.set((7, "tok"))
            return await dash._once("botguilds", factory)

        a, b = asyncio.ensure_future(call()), asyncio.ensure_future(call())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        return await a, await b, len(calls)

    assert asyncio.run(run()) == ("x", "x", 1)
