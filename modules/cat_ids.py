# path: modules/cat_ids.py

"""
Cat trade IDs. Every cat owner (per bot: the main bot and each clone are separate worlds) has ONE fixed ID made of
7 random words, e.g. `maple-otter-river-cloud-pepper-lantern-fox`. It never changes. Giving your ID to someone lets
them send you a trade offer from another server; nothing happens until you press Accept.

Who can trade with whom:
  * Free server:    only with members of the SAME server (the normal member picker). No IDs.
  * Premium server: also with ANYONE who has a trade ID on this bot, in any server (inter-server / global),
                    by typing their ID. The offer reaches them by DM.
The plan that counts is the one of the server where the trade is started, checked at that moment.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

WORD_COUNT = 7

WORDS = tuple(sorted(set("""
acorn amber anchor apple apricot arrow aster autumn badge bagel bamboo banjo barley basil beacon berry birch biscuit
blossom bluebell bonfire bramble breeze bridge brook bubble butter button cabin cactus canyon caramel carrot castle
cedar cherry chestnut cinder citrus clover cobalt cocoa comet compass coral cotton cricket crumb crystal cupcake daisy
dawn delta denim desert dewdrop dolphin dragonfly drizzle dune eagle ember emerald falcon feather fern fiddle finch
fjord flame flint flower forest fossil frost fudge galaxy garden garnet ginger glacier glimmer glow goblet granite
grape gravel grove guitar harbor hazel heather hedge heron hickory hollow honey horizon hummus iceberg indigo island
ivory jasmine jelly jigsaw juniper kettle kernel kiwi kite koala lagoon lantern lark laurel lavender lemon lilac lily
lime lotus lullaby magnet mango maple marble meadow melody meteor mint mirror mist mocha moon moss mountain muffin
mulberry nectar nettle nickel noodle nutmeg oasis ocean olive onyx orbit orchard orchid otter oyster paddle pancake
panda parrot pasture peach peanut pearl pebble pepper petal pickle pillow pine pixel planet plum pocket pollen pond
poppy prairie pretzel puddle pumpkin puzzle quartz quill quilt rabbit radish rainbow raisin raven reef ribbon river
robin rocket rose saffron sage sailor salmon sandal sapphire scarf season shadow shell shore silver sketch skylark
sleigh snow sparrow spice spiral sponge spruce squirrel star sticker stone storm strawberry stream sugar summer sunbeam
sunrise sunset sycamore tangerine teacup thimble thistle thunder tiger timber toast tomato trail tulip tundra turnip
turtle twilight umbrella valley vanilla velvet violet voyage waffle walnut waterfall whisper willow window winter
wizard wolf fox wonder yarn yogurt zebra zephyr zigzag
badger beetle bison bobcat bunny camel canary cheetah chipmunk cobweb cougar coyote crane dingo donkey duckling ferret
firefly gecko gibbon goat goose gopher hamster hedgehog hippo iguana jaguar kangaroo kitten lemur lizard llama lobster
lynx magpie marmot meerkat mole moose mouse narwhal newt ocelot octopus osprey owl pelican penguin pigeon pony puffin
quail raccoon reindeer rhino salamander seagull seal shark sloth snail swan toad toucan trout walrus weasel whale
wombat yak
""".split())))

assert len(WORDS) >= 256, "word list too small for 7-word IDs"

_SPLIT = re.compile(r"[\s,;._\-]+")


def generate() -> str:
    """7 random words (cryptographically random), lower case, joined with hyphens."""
    return "-".join(secrets.choice(WORDS) for _ in range(WORD_COUNT))


def normalize(text) -> Optional[str]:
    """What the person typed -> canonical ID, or None when it isn't exactly 7 known words.
    Case, spaces, commas and hyphens don't matter."""
    parts = [p for p in _SPLIT.split(str(text or "").strip().lower()) if p]
    if len(parts) != WORD_COUNT or any(p not in _WORD_SET for p in parts):
        return None
    return "-".join(parts)


_WORD_SET = frozenset(WORDS)


def can_trade_across_servers(premium: bool) -> bool:
    return bool(premium)


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def get_or_create(owner_id: int, clone_id) -> str:
    """The owner's fixed ID (created on first use). Safe against two requests racing."""
    pool = await _pool()
    async with pool.acquire() as conn:
        for _ in range(8):
            have = await conn.fetchval(
                "SELECT trade_id FROM cat_owner_ids WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                owner_id, _key(clone_id))
            if have:
                return have
            await conn.execute(
                "INSERT INTO cat_owner_ids (owner_id, clone_id, trade_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                owner_id, clone_id, generate())
        raise RuntimeError("couldn't create a trade id")


async def safe_get(owner_id: int, clone_id) -> Optional[str]:
    try:
        return await get_or_create(owner_id, clone_id)
    except Exception:
        logger.exception("[cat] couldn't read the trade id of %s", owner_id)
        return None


async def find_owner(text, clone_id) -> Optional[int]:
    """Owner id for what the person typed (same bot only), or None."""
    tid = normalize(text)
    if tid is None:
        return None
    pool = await _pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval(
            "SELECT owner_id FROM cat_owner_ids WHERE trade_id = $1 AND COALESCE(clone_id, -1) = $2", tid, _key(clone_id))
    return int(v) if v is not None else None
