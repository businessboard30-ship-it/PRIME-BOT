"""20 more cat ranks (0-25), rank-based asking-price ranges, the seconds-long heist, and the market list cards."""
import asyncio
from pathlib import Path

import pytest

from modules import cat_adopt as ca, cat_life as cl, cat_market as cm, cat_rank as cr

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def cat(rank=0, rarity="common", cid=7, name="Whiskers", pool=9):
    return ca.Cat(id=cid, owner_id=1, clone_id=None, pool_id=pool, name=name, rarity=rarity, trait="Brave", cat_rank=rank)


# ---- ranks ----
def test_there_are_26_ranks_with_unique_titles_and_emojis():
    assert cr.MAX_RANK == 25
    assert len(cr.RANK_TITLES) == len(cr.RANK_EMOJI) == len(cr.BASE_COST) == 26
    assert len(set(cr.RANK_TITLES)) == 26 and len(set(cr.RANK_EMOJI)) == 26


def test_the_original_six_ranks_are_unchanged():
    assert cr.RANK_TITLES[:6] == ("Newcomer", "Rising", "Seasoned", "Champion", "Royal", "Legend")
    assert cr.BASE_COST[:6] == (0, 300, 900, 2400, 6000, 15000)


def test_new_ranks_cost_more_every_step_and_scale_with_rarity():
    costs = [cr.cost_to_reach(r, "common") for r in range(1, 26)]
    assert costs == sorted(costs) and len(set(costs)) == 25
    assert cr.cost_to_reach(25, "legendary") == cr.cost_to_reach(25, "common") * 5


def test_badge_and_clamp_cover_the_new_ranks():
    assert cr.badge(25).endswith("Rank 25 \u00b7 Ultimate") and cr.badge(6).endswith("Rank 6 \u00b7 Mythic")
    assert cr.clamp(30) == 25 and cr.clamp(-1) == 0


def test_a_rank_5_cat_can_now_rank_up_and_rank_25_is_the_top():
    assert cr.next_cost(cat(5)) == 17700 and cr.next_cost(cat(25)) is None


def test_migration_widens_the_check_to_25_and_is_wired():
    sql = (ROOT / "database/migrations/056_cat_rank_25.sql").read_text()
    assert "BETWEEN 0 AND 25" in sql and "DROP CONSTRAINT" in sql
    db = (ROOT / "database.py").read_text()
    assert "056_cat_rank_25.sql" in db and 'SCHEMA_VERSION = "105"' in db


# ---- allowed prices ----
def test_price_range_grows_with_rank_and_rarity():
    lows = [cm.price_range("common", r)[0] for r in range(0, 26)]
    highs = [cm.price_range("common", r)[1] for r in range(0, 26)]
    assert lows == sorted(lows) and highs == sorted(highs)
    for r in (0, 5, 12, 25):
        assert cm.price_range("legendary", r)[0] > cm.price_range("common", r)[0]
        assert cm.price_range("legendary", r)[1] > cm.price_range("common", r)[1]


def test_price_range_is_always_valid_and_inside_the_global_limits():
    for rarity in ("common", "uncommon", "rare", "legendary", "weird"):
        for r in range(0, 26):
            low, high = cm.price_range(rarity, r)
            assert cm.MIN_PRICE <= low < high <= cm.MAX_PRICE


def test_price_ok_blocks_cheap_top_rank_cats_and_overpriced_new_ones():
    assert cm.price_ok("common", 0, 100) and not cm.price_ok("common", 0, 100_000)
    assert not cm.price_ok("legendary", 25, 1_000) and cm.price_ok("legendary", 25, 5_000_000)


def test_price_choices_are_inside_the_range_sorted_and_unique():
    for rarity in ("common", "legendary"):
        for r in (0, 3, 10, 25):
            low, high = cm.price_range(rarity, r)
            ch = cm.price_choices(rarity, r)
            assert ch == sorted(set(ch)) and ch[0] == low and ch[-1] <= high and all(low <= p <= high for p in ch)


def test_create_listing_refuses_a_price_outside_the_cats_range(monkeypatch):
    class Conn:
        def transaction(self):
            class T:
                async def __aenter__(s): return None
                async def __aexit__(s, *a): return False
            return T()
        async def fetchrow(self, q, *a):
            return {"owner_id": 1, "clone_id": None, "rarity": "legendary", "cat_rank": 25}
        async def fetchval(self, q, *a): return None
    class Acq:
        async def __aenter__(s): return Conn()
        async def __aexit__(s, *a): return False
    class Pool:
        def acquire(self): return Acq()
    async def _p(): return Pool()
    monkeypatch.setattr(cm, "_pool", _p)
    assert run(cm.create_listing(7, 1, None, 500, 1, 2)) == ("out_of_range", None)


# ---- heist in seconds ----
def test_heists_take_seconds_not_minutes():
    assert all(h.seconds <= 30 for h in cl.HEISTS.values())
    assert sorted(h.seconds for h in cl.HEISTS.values()) == [h.seconds for h in cl.HEISTS.values()]   # harder = a bit longer


def test_heist_screen_lists_seconds():
    pytest.importorskip("discord")
    from discord_bot.cogs import _views_cat_life as v
    text = v._outing_embed("heist").description
    assert "8s" in text and "25s" in text and "m \u00b7" not in text


# ---- market (Components V2) ----
class FakeUser:
    display_name = "Sam"
    class display_avatar:
        url = "https://cdn.discordapp.com/avatars/1/a.png"


class FakeClient:
    def get_user(self, i): return FakeUser()


def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(v) for v in payload)
    return 0


def _listings(k):
    return [cm.Listing(i, 100 + i, 1, None, 500 * i, 1, 2, 3, "open", cat(rank=i, cid=100 + i, name=f"Cat{i}", pool=i))
            for i in range(1, k + 1)]


def _texts(view):
    out = []
    def walk(items):
        for it in items:
            if hasattr(it, "content"):
                out.append(it.content)
            walk(getattr(it, "children", []) or [])
    walk(view.children)
    return "\n".join(out)


def _market(monkeypatch, k=4, pics=True, who=FakeClient()):
    from discord_bot.cogs import _views_cat_market as vm
    async def url_for(client, pool_id):
        if not pics: raise RuntimeError("no picture")
        return f"https://img/{pool_id}.png"
    async def list_open(page=0, size=4): return _listings(k), k
    monkeypatch.setattr(vm.cat_pool, "url_for", url_for)
    monkeypatch.setattr(vm.cm, "list_open", list_open)
    vm._SELLER_CACHE.clear()
    view = vm.MarketView(who, type("U", (), {"id": 1})())
    run(view.load())
    return vm, view


def test_market_is_a_v2_layout_with_a_cat_picture_and_seller_avatar_per_cat(monkeypatch):
    discord = pytest.importorskip("discord")
    vm, view = _market(monkeypatch)
    assert isinstance(view, discord.ui.LayoutView)
    text = _texts(view)
    for i in range(1, 5):
        assert f"Cat{i}" in text and f"{500 * i:,}" in text
    assert text.count("Sam") == 4
    comps = view.to_components()
    flat = repr(comps)
    assert flat.count("https://img/") == 4 and flat.count("avatars/1/a.png") == 4      # one cat picture + one avatar each


def test_a_full_market_page_stays_under_discords_40_component_limit(monkeypatch):
    pytest.importorskip("discord")
    vm, view = _market(monkeypatch, k=vm_page())
    n = _count(view.to_components())
    assert 25 <= n <= 40, n                       # a full page really is big (not a vacuous count), and still fits


def vm_page():
    from discord_bot.cogs import _views_cat_market as vm
    return vm.MARKET_PAGE


def test_market_survives_a_missing_seller_and_picture(monkeypatch):
    pytest.importorskip("discord")
    class Nobody:
        def get_user(self, i): return None
        async def fetch_user(self, i): raise RuntimeError("unknown")
    vm, view = _market(monkeypatch, k=1, pics=False, who=Nobody())
    assert "Seller 1" in _texts(view) and "Cat1" in _texts(view)


def test_empty_market_says_so_and_has_no_picker(monkeypatch):
    pytest.importorskip("discord")
    vm, view = _market(monkeypatch, k=0)
    assert "No cats are for sale" in _texts(view)


def test_detail_screen_has_big_picture_seller_avatar_and_adopt_button(monkeypatch):
    discord = pytest.importorskip("discord")
    vm, view = _market(monkeypatch, k=1)
    l = view.listings[0]
    detail = vm.MarketDetailView(view, l, view.seen[l.id])
    assert isinstance(detail, discord.ui.LayoutView)
    flat = repr(detail.to_components())
    assert "https://img/1.png" in flat and "avatars/1/a.png" in flat and "Adopt for 500 coins" in flat
    assert "Cat1" in _texts(detail)


def test_sell_wizard_steps_show_the_allowed_range():
    pytest.importorskip("discord")
    from discord_bot.cogs import _views_cat_market as vm
    c = cat(rank=5, rarity="rare", name="Mochi")
    low, high = cm.price_range("rare", 5)
    wiz = vm.SellWizard(c, 1)
    assert f"{low:,}" in wiz.price_embed().description and f"{high:,}" in wiz.price_embed().description
    assert "step 1 of 2" in wiz.price_embed().title
    wiz.price = low
    assert "step 2 of 2" in wiz.confirm_embed().title and f"{low:,}" in wiz.confirm_embed().description
