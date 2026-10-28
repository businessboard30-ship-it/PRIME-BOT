# path: tests/integration/test_card_hub_postgres.py
"""The Card Hub driven end to end against real Postgres with fake Discord interactions: Store -> buy -> collection ->
boost -> profile -> rankings, plus the persistence guarantees (no timeout, every control rebuilt from its custom_id alone
after a 'restart', component ceiling, Components V2 only)."""
import asyncio
import re
from types import SimpleNamespace

import discord
import pytest

from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from discord_bot.cogs import _views_card_hub as h
from modules import card_boost as cb
from modules import card_economy as ce
from modules import leveling

DIR = MIGRATION.parent
GUILD = 555


async def _up(pool):
    async with pool.acquire() as c:
        for f in ("089_card_boosts.sql", "090_card_trading.sql", "091_card_coin_games.sql", "092_card_profiles.sql"):
            await c.execute((DIR / f).read_text())
        await c.execute("CREATE TABLE IF NOT EXISTS discord_xp (guild_id BIGINT, user_id BIGINT, clone_id INTEGER, "
                        "total_xp BIGINT NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0)")


def _xp_for_level(level):
    xp = 0
    while leveling.compute_level(xp) < level:
        xp += 25
    return xp


async def _give_level(pool, uid, level):
    async with pool.acquire() as c:
        await c.execute("DELETE FROM discord_xp WHERE user_id=$1", uid)
        await c.execute("INSERT INTO discord_xp (guild_id, user_id, total_xp) VALUES ($1,$2,$3)", GUILD, uid, _xp_for_level(level))


class FakeUser:
    def __init__(self, uid, name=None, bot=False):
        self.id, self.display_name, self.bot = uid, name or f"user{uid}", bot
        self.display_avatar = SimpleNamespace(url=f"https://cdn.example/{uid}.png")


class FakeInteraction:
    def __init__(self, uid, known=()):
        self.user = FakeUser(uid)
        users = {u.id: u for u in known}
        self.guild = SimpleNamespace(id=GUILD, name="Test Guild", members=[FakeUser(1), FakeUser(2), FakeUser(3)],
                                     get_member=lambda i: next((m for m in self.guild.members if m.id == i), None))
        self.client = SimpleNamespace(get_user=lambda i: users.get(i), fetch_user=self._fetch)
        self._users = users
        self.view = None
        self.messages = []
        self.modal = None
        self._done = False
        self.response = SimpleNamespace(defer=self._defer, is_done=lambda: self._done, send_modal=self._modal,
                                        send_message=self._send)
        self.followup = SimpleNamespace(send=self._send)

    async def _fetch(self, i):
        if i in self._users:
            return self._users[i]
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "unknown")

    async def _defer(self, **kw):
        self._done = True

    async def _modal(self, m):
        self._done = True
        self.modal = m

    async def _send(self, content=None, **kw):
        self._done = True
        self.messages.append(content or "")

    async def edit_original_response(self, **kw):
        self.view = kw.get("view")


def text(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def controls(view):
    return [c for c in view.walk_children() if getattr(c, "custom_id", None)]


def find(view, prefix):
    return [c for c in controls(view) if c.custom_id.startswith(prefix)]


async def click(uid, act, arg="", value=None, known=()):
    fi = FakeInteraction(uid, known)
    await h.dispatch(fi, act, None, GUILD, arg, value)
    return fi


async def _card(pool, name, rarity="rare", base=100, stock=None, offers=((("permanent", None, 500)),), **kw):
    async with pool.acquire() as c:
        cid = await c.fetchval(
            "INSERT INTO card_catalog (name, rarity, store_enabled, base_strength, min_level, max_level, skills, "
            "max_boost_level) VALUES ($1,$2,TRUE,$3,$4,$5,$6::jsonb,10) RETURNING card_id",
            name, rarity, base, kw.get("min_level", 0), kw.get("max_level"),
            '[{"key":"s1","name":"Guard","unlock_level":2}]')
    if stock is not None:
        await ce.set_limited_stock(cid, stock)
    ids = []
    for kind, days, price in offers:
        ids.append(await ce.create_offer(cid, kind, price, days))
    return cid, ids


@pytest.mark.asyncio
async def test_home_is_components_v2_and_shows_the_numbers(pool):
    await _up(pool)
    await _fund(pool, 1, 1234)
    await _give_level(pool, 1, 3)
    fi = await click(1, "home")
    v = fi.view
    assert isinstance(v, discord.ui.LayoutView) and v.has_components_v2() and v.timeout is None
    t = text(v)
    assert "1,234" in t and "Level:** 3" in t


@pytest.mark.asyncio
async def test_store_labels_stock_and_hides_sold_out_from_the_picker(pool):
    await _up(pool)
    unl, (o_unl,) = await _card(pool, "Pebble", "common", 10)
    lim, (o_lim,) = await _card(pool, "Dragon", "god", 900, stock=1, offers=(("permanent", None, 100),))
    _, (o_rent,) = await _card(pool, "Wisp", "rare", 50, offers=(("rental", 7, 30),))
    await _fund(pool, 1, 1000)
    fi = await click(1, "store", "p0")
    t = text(fi.view)
    assert "Unlimited" in t and "1/1 left" in t and "Rent 7d 30" in t
    assert any(str(o_lim) == opt.value for c in find(fi.view, "chubs:pick") for opt in c.item.options)
    await ce.buy_offer(1, None, o_lim, 50)                                   # last copy sold
    fi = await click(1, "store", "p0")
    assert "OUT OF STOCK" in text(fi.view)
    values = [opt.value for c in find(fi.view, "chubs:pick") for opt in c.item.options]
    assert str(o_lim) not in values and str(o_unl) in values


@pytest.mark.asyncio
async def test_buy_flow_and_double_click_buys_once(pool):
    await _up(pool)
    cid, (offer,) = await _card(pool, "Pebble", "common", 10, offers=(("permanent", None, 200),))
    await _fund(pool, 1, 500)
    fi = await click(1, "pick", "p0", str(offer))
    assert "Confirm purchase" in text(fi.view)
    ok = find(fi.view, "chub:buyok")[0]
    assert not ok.item.disabled
    a, b = await asyncio.gather(click(1, "buyok", f"o{offer}.p0"), click(1, "buyok", f"o{offer}.p0"))
    assert await _bal(pool, 1) == 300                                          # paid once, not twice
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_copies WHERE owner_id=1") == 1
    assert "Bought" in text((a.view or b.view))


@pytest.mark.asyncio
async def test_cannot_afford_level_gated_and_stale_offers_charge_nothing(pool):
    await _up(pool)
    _, (poor,) = await _card(pool, "Pricey", "legendary", 100, offers=(("permanent", None, 5000),))
    _, (gated,) = await _card(pool, "Elite", "epic", 100, min_level=10, offers=(("permanent", None, 10),))
    await _fund(pool, 1, 100)
    await _give_level(pool, 1, 2)
    fi = await click(1, "pick", "p0", str(poor))
    assert find(fi.view, "chub:buyok")[0].item.disabled and "more coins" in text(fi.view)
    fi = await click(1, "buyok", f"o{poor}.p0")                                # a stale/forged Confirm still can't buy
    assert "Not enough coins" in text(fi.view)
    fi = await click(1, "pick", "p0", str(gated))
    assert "Needs level 10" in text(fi.view)
    fi = await click(1, "buyok", f"o{gated}.p0")
    assert "Requires level 10" in text(fi.view)
    fi = await click(1, "buyok", "o99999.p0")
    assert "no longer available" in text(fi.view)
    assert await _bal(pool, 1) == 100
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_copies") == 0


@pytest.mark.asyncio
async def test_collection_detail_boost_and_skill_unlock(pool):
    await _up(pool)
    cid, (offer,) = await _card(pool, "Pebble", "common", 100, offers=(("permanent", None, 50),))
    await _fund(pool, 1, 1000)
    r = await ce.buy_offer(1, None, offer, 50)
    copy = r["copy_id"]
    fi = await click(1, "col", "p0.s0.r0")
    assert "Pebble" in text(fi.view) and "1 copy" in text(fi.view)
    fi = await click(1, "inspect", "p0.s0.r0", str(copy))
    t = text(fi.view)
    assert "boost **0/10**" in t and "base 100" in t and "unlocks at boost 2" in t
    fi = await click(1, "boost", f"c{copy}.i0.p0.s0.r0")
    assert "Boosted to **1**" in text(fi.view)
    fi = await click(1, "boost", f"c{copy}.i0.p0.s0.r0")
    t = text(fi.view)
    assert "Boosted to **2**" in t and "✅ **Guard**" in t                       # skill unlocked at the milestone
    assert await _bal(pool, 1) == 1000 - 50 - cb.coin_cost("common", 0) - cb.coin_cost("common", 1)
    other = await click(2, "inspect", "p0.s0.r0", str(copy))                     # someone else's card is not theirs
    assert "isn't in your collection" in text(other.view)
    other = await click(2, "boost", f"c{copy}.i0.p0.s0.r0")
    assert "isn't in your collection" in text(other.view) or "don't own" in text(other.view)


@pytest.mark.asyncio
async def test_boost_items_required_for_high_levels_and_never_double_spent(pool):
    await _up(pool)
    cid, (offer,) = await _card(pool, "Pebble", "common", 100, offers=(("permanent", None, 1),))
    await _fund(pool, 1, 10**7)
    copy = (await ce.buy_offer(1, None, offer, 50))["copy_id"]
    for _ in range(5):
        await cb.boost_card(1, None, copy)
    fi = await click(1, "inspect", "p0.s0.r0", str(copy))                        # level 6 needs a core: coins alone disabled
    boost_btn = find(fi.view, "chub:boost")[0]
    assert boost_btn.item.disabled and "Boost Core" in text(fi.view)
    await cb.grant_item(1, None, "boost_core", 1)
    fi = await click(1, "inspect", "p0.s0.r0", str(copy))
    item_btn = [c for c in find(fi.view, "chub:boost") if "Core" in (c.item.label or "")]
    assert item_btn
    arg = item_btn[0].custom_id.split(":", 4)[4]
    a, b = await asyncio.gather(click(1, "boost", arg), click(1, "boost", arg))   # double click: one item, one level
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT boost_level FROM card_copies WHERE copy_id=$1", copy) == 6
        assert await c.fetchval("SELECT quantity FROM user_boost_items WHERE user_id=1") == 0


@pytest.mark.asyncio
async def test_rented_cards_cannot_be_boosted_or_featured(pool):
    await _up(pool)
    _, (offer,) = await _card(pool, "Wisp", "rare", 50, offers=(("rental", 7, 10),))
    await _fund(pool, 1, 1000)
    copy = (await ce.buy_offer(1, None, offer, 50))["copy_id"]
    fi = await click(1, "inspect", "p0.s0.r0", str(copy))
    assert "Rental" in text(fi.view) and not find(fi.view, "chub:boost") and not find(fi.view, "chub:feature")
    fi = await click(1, "boost", f"c{copy}.i0.p0.s0.r0")
    assert "Rented cards can't be boosted" in text(fi.view)


@pytest.mark.asyncio
async def test_profile_privacy_signature_lookup_and_achievements(pool):
    await _up(pool)
    _, (offer,) = await _card(pool, "Pebble", "common", 100, offers=(("permanent", None, 1),))
    await _fund(pool, 1, 1000)
    await _fund(pool, 2, 777)
    copy = (await ce.buy_offer(1, None, offer, 50))["copy_id"]
    await click(1, "feature", f"c{copy}.p0.s0.r0")
    fi = await click(1, "prof")
    t = text(fi.view)
    assert "Signature" in t and "Pebble" in t and "999" in t and "1 copy" in t
    # privacy: strangers see only what the owner shows
    other = FakeInteraction(2)
    other.guild.get_member = lambda i: None
    view = await h.screen_prof(h.Ctx(other, None, GUILD), 1)
    assert "🔒 private" in text(view)                                           # coins hidden by default
    await click(1, "priv", "show_collection")                                   # owner hides the collection
    view = await h.screen_prof(h.Ctx(other, None, GUILD), 1)
    assert "keeps their collection private" in text(view)
    # lookup: a Discord ID of a real person works, a bot / unknown does not, a wrong ID is rejected nicely
    fi = FakeInteraction(1, known=[FakeUser(2), FakeUser(999999999999999990, bot=True)])
    uid, err = await h.resolve_user_ref(fi, "222222222222222222")                  # nobody has this ID
    assert uid is None and "can't find" in err
    uid, err = await h.resolve_user_ref(fi, "999999999999999990")                  # a bot is not a collector
    assert uid is None and "can't find" in err
    uid, err = await h.resolve_user_ref(fi, "<@999999999999999999>")
    assert uid is None
    fi2 = FakeInteraction(1, known=[FakeUser(222222222222222222)])
    assert await h.resolve_user_ref(fi2, "222222222222222222") == (222222222222222222, None)
    assert (await h.resolve_user_ref(fi2, "hello"))[0] is None
    assert (await h.resolve_user_ref(fi2, str(1)))[0] is None
    fi = await click(1, "ach")
    assert "Achievements" in text(fi.view) and "never pay coins" in text(fi.view)


@pytest.mark.asyncio
async def test_rankings_local_vs_global_and_rentals_excluded(pool):
    await _up(pool)
    _, (perm,) = await _card(pool, "Pebble", "common", 100, offers=(("permanent", None, 1),))
    _, (rent,) = await _card(pool, "Wisp", "rare", 50, offers=(("rental", 7, 1),))
    for u in (1, 2, 77):
        await _fund(pool, u, 1000)
    await ce.buy_offer(1, None, perm, 50)
    await ce.buy_offer(1, None, perm, 50)
    await ce.buy_offer(2, None, perm, 50)
    await ce.buy_offer(77, None, perm, 50)                                       # not in the fake guild
    await ce.buy_offer(2, None, rent, 50)                                        # rentals never count
    fi = await click(1, "ranks", "m1.l1")                                        # copies, this server
    t = text(fi.view)
    assert "Most card copies" in t and "user1" in t and "user2" in t and "User 77" not in t
    assert re.search(r"user1 · \*\*2\*\*", t) and re.search(r"user2 · \*\*1\*\*", t)
    fi = await click(1, "ranks", "m1.l0")
    assert "User 77" in text(fi.view) and "every server" in text(fi.view)
    fi = await click(1, "rankm", "m0.l1", "5")
    assert "Rarest collection" in text(fi.view)


@pytest.mark.asyncio
async def test_every_screen_is_persistent_v2_within_limits_and_survives_a_restart(pool):
    """The restart test: render every screen, then rebuild EVERY control from nothing but its custom_id (what discord.py
    does on a fresh boot via add_dynamic_items) and make sure each one still routes and renders."""
    await _up(pool)
    _, (o1, _rent) = await _card(pool, "Pebble", "common", 100, offers=(("permanent", None, 50), ("rental", 7, 10)))
    _, (o2,) = await _card(pool, "Dragon", "god", 900, stock=3, offers=(("permanent", None, 80),))
    await _fund(pool, 1, 10**6)
    copy = (await ce.buy_offer(1, None, o1, 50))["copy_id"]
    await ce.buy_offer(1, None, o2, 50)
    await cb.grant_item(1, None, "boost_shard", 2)
    shots = []
    for act, arg, val in [("home", "", None), ("store", "p0", None), ("store", "p5", None), ("pick", "p0", str(o1)),
                          ("col", "p0.s1.r0", None), ("col", "p0.s0.r4", None), ("inspect", "p0.s0.r0", str(copy)),
                          ("prof", "", None), ("ach", "", None), ("ranks", "m0.l1", None), ("ranks", "m6.l0", None)]:
        shots.append((act, (await click(1, act, arg, val)).view))
    tpls = {cls.__discord_ui_compiled_template__: cls for cls in h.DYNAMIC_ITEMS}
    seen = 0
    for act, view in shots:
        assert isinstance(view, discord.ui.LayoutView) and view.has_components_v2(), act
        assert view.timeout is None and view.is_persistent(), act            # no arbitrary timeout, restart-safe
        assert view.total_children_count <= 40, (act, view.total_children_count)
        assert not any(isinstance(c, discord.ui.Button) and c.url for c in view.walk_children())
        for c in controls(view):
            cid = c.custom_id
            assert len(cid) <= 100, cid
            if cid.startswith("chub_pg:"):
                assert c.item.disabled if hasattr(c, "item") else c.disabled      # inert page counter
                continue
            m = next((t.fullmatch(cid) for t in tpls if t.fullmatch(cid)), None)
            assert m, f"{cid} would not be routed after a restart"
            cls = tpls[m.re]
            rebuilt = await cls.from_custom_id(FakeInteraction(1), None, m)  # a fresh process knows only the custom_id
            assert rebuilt.custom_id == cid
            seen += 1
            if act == "home" or cid.startswith("chub:") and not cid.startswith("chub:buyok") and "boost" not in cid \
                    and "feature" not in cid and "priv" not in cid and "lookup" not in cid:
                if cid.startswith("chubs:"):
                    value = (c.item.options[0].value)
                    if value == "x":
                        continue
                else:
                    value = None
                fi = FakeInteraction(1)
                await h.dispatch(fi, m["act"], h._cf(m["c"]), int(m["g"]), m["arg"], value)
                assert fi.view is not None and not fi.messages, (cid, fi.messages)
    assert seen > 30


@pytest.mark.asyncio
async def test_a_failing_screen_leaves_the_hub_usable(pool, monkeypatch):
    await _up(pool)

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(h.ce, "list_store", boom)
    fi = await click(1, "store", "p0")
    assert fi.view is None and fi.messages and "went wrong" in fi.messages[0]
    fi = await click(1, "home")                                                  # other screens still work
    assert fi.view is not None
    fi = await click(1, "nobody_knows_this_action")                              # unknown action falls back to home
    assert fi.view is not None
