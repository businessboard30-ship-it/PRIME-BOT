# path: modules/cat_ids.py

"""
Cat trade IDs. Every cat owner (per bot: the main bot and each clone keep separate collections) has ONE fixed short ID:
6 digits and a check letter, e.g. `142574D`. It never changes. Giving your ID to someone lets them send you a trade offer;
nothing happens until you press Accept.

The last letter is a check character worked out from the digits, so a mistyped or mis-copied ID is caught at once
("that doesn't look right") without touching the database, and a guess has a 1-in-23 chance of even looking valid.
The letters I, L and O are never used (they look like 1 and 0).

Older owners had an ID of 7 random words. Those keep working when typed (nobody's old ID breaks) but are no longer shown;
everyone gets a short ID the next time they open My cats.

Who can trade with whom:
  * Free server:    only with members of the SAME server (the normal member picker, or their ID for a member).
  * Premium server: also with ANYONE who has an ID on this bot, in any server (inter-server / global),
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

# ---- short IDs: 6 digits + 1 check letter ----
SHORT_DIGITS = 6
SHORT_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"          # 23 letters (a prime): no I, L, O
_WEIGHTS = (7, 3, 5, 2, 9, 4)
_SHORT_IGNORE = re.compile(r"[\s#\-_.,]+")
_SHORT_RE = re.compile(r"[1-9][0-9]{5}[A-Z]")


def check_letter(digits: str) -> str:
    """The letter that closes a 6-digit number. Because 23 is prime, any single wrong digit and any swap of two
    neighbouring different digits always changes it, so those typos are always caught."""
    total = sum(int(d) * w for d, w in zip(digits, _WEIGHTS))
    return SHORT_LETTERS[total % len(SHORT_LETTERS)]


def generate_short() -> str:
    """A random short ID, e.g. `142574D` (cryptographically random, never starts with 0)."""
    digits = str(100000 + secrets.randbelow(900000))
    return digits + check_letter(digits)


def normalize_short(text) -> Optional[str]:
    """What the person typed -> canonical short ID, or None when it isn't one (wrong shape or wrong check letter).
    Case, spaces, `#`, dashes and dots don't matter."""
    t = _SHORT_IGNORE.sub("", str(text or "")).upper()
    if not _SHORT_RE.fullmatch(t) or check_letter(t[:SHORT_DIGITS]) != t[SHORT_DIGITS]:
        return None
    return t


def parse(text):
    """('short', id) for a short ID, ('words', id) for an old 7-word ID, else None."""
    sid = normalize_short(text)
    if sid:
        return ("short", sid)
    words = normalize(text)
    return ("words", words) if words else None


# ---- lookups: a person who keeps typing IDs that don't exist is slowed down (nobody can scan the ID space) ----
LOOKUP_FAILS_MAX = 8
LOOKUP_WINDOW_SECONDS = 3600
_fails: dict = {}


def lookup_allowed(user_id: int) -> bool:
    import time
    now = time.monotonic()
    recent = [t for t in _fails.get(user_id, []) if now - t < LOOKUP_WINDOW_SECONDS]
    _fails[user_id] = recent
    return len(recent) < LOOKUP_FAILS_MAX


def note_failed_lookup(user_id: int) -> None:
    import time
    _fails.setdefault(user_id, []).append(time.monotonic())


def can_trade_across_servers(premium: bool) -> bool:
    return bool(premium)


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def get_or_create(owner_id: int, clone_id) -> str:
    """The owner's fixed SHORT ID (created on first use; an owner who only had a 7-word ID gets a short one now).
    Safe against two requests racing and against the (rare) clash with somebody else's number."""
    pool = await _pool()
    async with pool.acquire() as conn:
        for _ in range(12):
            have = await conn.fetchval(
                "SELECT short_id FROM cat_owner_ids WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                owner_id, _key(clone_id))
            if have:
                return have
            sid = generate_short()
            try:
                # An owner who only has an old 7-word ID gets a short one added to the same row...
                done = await conn.execute(
                    "UPDATE cat_owner_ids SET short_id = $3 WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND short_id IS NULL",
                    owner_id, _key(clone_id), sid)
                if str(done).endswith(" 0"):
                    # ...a new owner gets a row. DO NOTHING also quietly skips a number somebody else has: we just draw again.
                    await conn.execute(
                        "INSERT INTO cat_owner_ids (owner_id, clone_id, short_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        owner_id, clone_id, sid)
            except Exception as e:                       # the UPDATE hit a number somebody else has: draw again
                if "unique" in str(e).lower() or "ux_cat_owner_ids_short" in str(e):
                    continue
                raise
        raise RuntimeError("couldn't create a trade id")


async def safe_get(owner_id: int, clone_id) -> Optional[str]:
    try:
        return await get_or_create(owner_id, clone_id)
    except Exception:
        logger.exception("[cat] couldn't read the trade id of %s", owner_id)
        return None


_WHERE = {"short": "short_id", "words": "trade_id"}


async def find_owner(text, clone_id) -> Optional[int]:
    """Owner id for what the person typed (same bot only), or None."""
    parsed = parse(text)
    if parsed is None:
        return None
    column = _WHERE[parsed[0]]
    pool = await _pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval(
            f"SELECT owner_id FROM cat_owner_ids WHERE {column} = $1 AND COALESCE(clone_id, -1) = $2", parsed[1], _key(clone_id))
    return int(v) if v is not None else None


async def find_identity(text):
    """(owner_id, clone_id) for what the person typed (a short ID, or an old 7-word one), looked up across the main bot
    AND every clone, or None. clone_id says which bot's collection that person's cats are in (None = main bot)."""
    parsed = parse(text)
    if parsed is None:
        return None
    column = _WHERE[parsed[0]]
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT owner_id, clone_id FROM cat_owner_ids WHERE {column} = $1 ORDER BY created_at LIMIT 1", parsed[1])
    return (int(row["owner_id"]), row["clone_id"]) if row is not None else None
