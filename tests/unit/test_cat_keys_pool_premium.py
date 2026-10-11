"""/cat: several API keys (rotation, per-key budget, pausing), the shared picture pool, Premium perks."""
import asyncio
from collections import deque

import discord
import pytest

from discord_bot.cogs import cat


def run(coro):
    return asyncio.run(coro)


def url(i):
    return f"https://cdn2.thecatapi.com/images/{i}.jpg"


class Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status, self.payload, self.headers = status, payload, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.payload


class Session:
    """Answers each GET from a function (url, headers) -> Resp, and records the calls."""
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def get(self, u, headers=None, timeout=None):
        self.calls.append((u, dict(headers or {})))
        r = self.fn(u, dict(headers or {}))
        if isinstance(r, Exception):
            raise r
        return r

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def close(self):
        pass


def cats(n, start=0):
    return Resp(200, [{"url": url(start + i)} for i in range(n)])


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(cat.config, "CAT_API_KEYS", (), raising=False)
    monkeypatch.setattr(cat.config, "CAT_API_KEY", "", raising=False)
    monkeypatch.setattr(cat.config, "CAT_KEY_RPM", 100, raising=False)
    cat.reset_state()
    yield
    cat.reset_state()


def keys(monkeypatch, *ks, rpm=100):
    monkeypatch.setattr(cat.config, "CAT_API_KEYS", tuple(ks), raising=False)
    monkeypatch.setattr(cat.config, "CAT_KEY_RPM", rpm, raising=False)


# ---------- the key pool ----------
def test_no_key_means_one_picture_per_request_and_a_small_budget():
    s = Session(lambda u, h: cats(1))
    assert run(cat.fetch_cat_urls(1, session=s)) == [url(0)]
    assert "limit=1" in s.calls[0][0] and "x-api-key" not in s.calls[0][1]
    assert cat.key_pool().keys[0].rpm == cat.ANON_RPM


def test_with_a_key_one_request_fetches_a_whole_batch_and_serves_from_it(monkeypatch):
    keys(monkeypatch, "k1")
    s = Session(lambda u, h: cats(25))
    got = [run(cat.fetch_cat_urls(1, session=s))[0] for _ in range(10)]
    assert len(set(got)) == 10 and len(s.calls) == 1                 # ten cats, ONE api request
    assert "limit=25" in s.calls[0][0] and s.calls[0][1] == {"x-api-key": "k1"}
    assert cat.pool_stats()["pool"] == 15


def test_requests_are_spread_across_keys(monkeypatch):
    keys(monkeypatch, "k1", "k2", "k3")
    seen = []

    def fn(u, h):
        seen.append(h["x-api-key"])
        return cats(25, start=len(seen) * 100)
    s = Session(fn)
    for _ in range(3):
        cat._general.clear()
        run(cat.fetch_cat_urls(1, session=s))
    assert sorted(seen) == ["k1", "k2", "k3"]


def test_a_key_over_its_minute_budget_is_skipped(monkeypatch):
    keys(monkeypatch, "k1", "k2", rpm=2)
    p = cat.key_pool()
    now = 1000.0
    picked = [p.acquire(now).secret for _ in range(4)]
    assert sorted(picked) == ["k1", "k1", "k2", "k2"] and p.acquire(now) is None     # both spent
    assert p.acquire(now + 61) is not None                                            # window moves on


def test_429_pauses_that_key_and_the_same_request_retries_on_another(monkeypatch):
    keys(monkeypatch, "k1", "k2")

    def fn(u, h):
        return Resp(429, None, {"Retry-After": "120"}) if h["x-api-key"] == "k1" else cats(5)
    s = Session(fn)
    cat.key_pool().keys[1].hits.append(10 ** 9)           # k2 looks busier, so k1 is picked first
    got = run(cat.fetch_cat_urls(1, session=s))
    assert got == [url(0)]
    assert {c[1]["x-api-key"] for c in s.calls} == {"k1", "k2"}
    assert any(st["paused"] for st in cat.key_pool().stats() if st["key"] == "key #1")


def test_401_pauses_a_rejected_key_for_an_hour_and_never_logs_it(monkeypatch, caplog):
    keys(monkeypatch, "SECRET-KEY-ONE", "k2")
    s = Session(lambda u, h: Resp(401) if h["x-api-key"] == "SECRET-KEY-ONE" else cats(3))
    with caplog.at_level("DEBUG"):
        for _ in range(3):
            cat._general.clear()
            run(cat.fetch_cat_urls(1, session=s))
    k1 = next(k for k in cat.key_pool().keys if k.secret == "SECRET-KEY-ONE")
    assert k1.blocked_until - __import__("time").monotonic() > 3000
    assert "SECRET-KEY-ONE" not in caplog.text and "k2" not in caplog.text      # only 'key #1' style labels


def test_all_keys_exhausted_falls_back_without_error(monkeypatch):
    keys(monkeypatch, "k1", rpm=1)
    cat.key_pool().acquire()                                      # budget spent
    calls = []

    def fn(u, h):
        calls.append(u)
        return Resp(200, {"url": "https://cataas.com/cat/abc"})
    got = run(cat.fetch_cat_urls(2, session=Session(fn)))
    assert got and all(cat.safe_image_url(u) for u in got) and all("cataas.com" in c for c in calls)


def test_bad_urls_in_a_batch_are_dropped(monkeypatch):
    keys(monkeypatch, "k1")
    s = Session(lambda u, h: Resp(200, [{"url": url(1)}, {"url": "http://evil.example/x.jpg"}, {"nourl": 1}, "junk"]))
    assert run(cat.fetch_cat_urls(3, session=s))[:1] == [url(1)]
    assert list(cat._general) == []


def test_pool_is_deduplicated_and_capped():
    d = deque()
    cat._extend(d, [url(1), url(1), url(2)])
    assert list(d) == [url(1), url(2)]
    cat._extend(d, [url(i) for i in range(1000)])
    assert len(d) == cat.POOL_MAX


def test_count_is_clamped_to_the_maximum(monkeypatch):
    keys(monkeypatch, "k1")
    s = Session(lambda u, h: cats(25))
    assert len(run(cat.fetch_cat_urls(99, session=s))) == cat.MAX_COUNT
    assert len(run(cat.fetch_cat_urls(0, session=s))) == 1


def test_background_topup_fills_a_low_pool(monkeypatch):
    keys(monkeypatch, "k1")
    s = Session(lambda u, h: cats(25))
    monkeypatch.setattr(cat.aiohttp, "ClientSession", lambda *a, **k: s)
    run(cat._topup())
    assert len(cat._general) == 25
    run(cat._topup())                                              # 25 >= REFILL_BELOW, so no second request
    assert len(s.calls) == 1


# ---------- breeds ----------
def test_breed_fetch_needs_a_key_and_never_substitutes_another_breed(monkeypatch):
    s = Session(lambda u, h: cats(5))
    assert run(cat.fetch_cat_urls(1, breed="beng", session=s)) == [] and not s.calls       # no key configured
    keys(monkeypatch, "k1")
    s2 = Session(lambda u, h: cats(5))
    got = run(cat.fetch_cat_urls(2, breed="beng", session=s2))
    assert len(got) == 2 and "breed_ids=beng" in s2.calls[0][0] and "limit=10" in s2.calls[0][0]
    empty = Session(lambda u, h: Resp(200, []))
    cat._by_breed.clear()
    assert run(cat.fetch_cat_urls(1, breed="zzzz", session=empty)) == []                  # no general-pool fallback


def test_breed_list_is_cached_validated_and_has_a_fallback(monkeypatch):
    keys(monkeypatch, "k1")
    payload = [{"id": "beng", "name": "Bengal"}, {"id": "BAD ID!", "name": "x"}, {"id": "siam", "name": "Siamese"}, {"id": "x"}]
    s = Session(lambda u, h: Resp(200, payload))
    assert run(cat.get_breeds(s)) == [("beng", "Bengal"), ("siam", "Siamese")]
    run(cat.get_breeds(s))
    assert len(s.calls) == 1                                                               # cached
    cat.reset_state()
    down = Session(lambda u, h: RuntimeError("down"))
    assert run(cat.get_breeds(down)) == cat.FALLBACK_BREEDS


def test_resolve_breed():
    b = [("beng", "Bengal"), ("bsho", "British Shorthair"), ("birm", "Birman"), ("siam", "Siamese")]
    assert cat.resolve_breed("beng", b) == ("beng", "Bengal")
    assert cat.resolve_breed("  SIAMESE ", b) == ("siam", "Siamese")
    assert cat.resolve_breed("brit", b) == ("bsho", "British Shorthair")
    assert cat.resolve_breed("b", b) is None            # ambiguous
    assert cat.resolve_breed("", b) is None and cat.resolve_breed("nope", b) is None


# ---------- Premium gating on /cat ----------
class FakeInteraction:
    def __init__(self, guild_id=5, uid=42):
        self.user = type("U", (), {"id": uid})()
        self.guild_id = guild_id
        self.client = type("C", (), {"clone_id": None})()
        self.sent, self.followups, self._done = [], [], False
        self.response = self
        self.followup = self

    async def defer(self, **k):
        self._done = True

    async def send_message(self, content=None, **k):
        self._done = True
        self.sent.append((content, k))

    async def send(self, content=None, **k):
        self.followups.append((content, k))


@pytest.fixture
def cmd(monkeypatch):
    state = {"premium": False, "fetched": []}

    async def prem(gid, clone_id=None):
        return state["premium"]
    monkeypatch.setattr(cat.db, "is_guild_premium_active", prem, raising=False)

    async def fake(count=1, breed=None, session=None):
        state["fetched"].append((count, breed))
        return [url(i) for i in range(count)]
    monkeypatch.setattr(cat, "fetch_cat_urls", fake)

    async def breeds(session=None):
        return [("beng", "Bengal"), ("siam", "Siamese")]
    monkeypatch.setattr(cat, "get_breeds", breeds)
    return state, cat.CatCog(None).cat.callback


def test_free_server_plain_cat_works_and_perks_get_a_friendly_nudge(cmd):
    state, f = cmd
    i = FakeInteraction()
    run(f(cat.CatCog(None), i))
    assert state["fetched"] == [(1, None)] and len(i.followups[0][1]["embeds"]) == 1
    for kw in ({"count": 2}, {"breed": "beng"}):
        cat._last_use.clear()
        j = FakeInteraction()
        run(f(cat.CatCog(None), j, **kw))
        assert "Premium" in j.sent[0][0] and j.sent[0][1]["ephemeral"] is True and not j.followups
    assert state["fetched"] == [(1, None)]                      # nothing fetched for the refused perks


def test_premium_server_gets_breed_multiple_cats_and_short_cooldown(cmd):
    state, f = cmd
    state["premium"] = True
    i = FakeInteraction()
    run(f(cat.CatCog(None), i, breed="siamese", count=3))
    assert state["fetched"] == [(3, "siam")]
    emb = i.followups[0][1]["embeds"]
    assert len(emb) == 3 and emb[0].title == "🐱 Siamese" and emb[1].title is None
    # premium cooldown is 2s, free is 5s: a use 3s ago is fine for premium only
    cat._last_use[42] -= 3
    run(f(cat.CatCog(None), FakeInteraction()))
    assert len(state["fetched"]) == 2
    state["premium"] = False
    cat._last_use[42] -= 3
    k = FakeInteraction()
    run(f(cat.CatCog(None), k))
    assert "another cat" in k.sent[0][0]


def test_unknown_breed_is_refused_politely_for_premium(cmd):
    state, f = cmd
    state["premium"] = True
    i = FakeInteraction()
    run(f(cat.CatCog(None), i, breed="dragon"))
    assert "don't know that breed" in i.sent[0][0] and not state["fetched"]


def test_dm_is_never_premium_and_a_failing_premium_check_means_free(cmd, monkeypatch):
    assert run(cat.is_premium(FakeInteraction(guild_id=None))) is False

    async def boom(gid, clone_id=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(cat.db, "is_guild_premium_active", boom, raising=False)
    assert run(cat.is_premium(FakeInteraction())) is False


def test_no_cat_available_says_sorry_with_breed_name(cmd, monkeypatch):
    state, f = cmd
    state["premium"] = True

    async def none(count=1, breed=None, session=None):
        return []
    monkeypatch.setattr(cat, "fetch_cat_urls", none)
    i = FakeInteraction()
    run(f(cat.CatCog(None), i, breed="beng"))
    assert "couldn't find a **Bengal**" in i.followups[0][0]


def test_command_signature_is_still_one_slash_command_with_two_optional_perks():
    c = cat.CatCog(None).get_app_commands()
    assert [x.name for x in c] == ["cat"]
    params = {p.name: p.required for p in c[0].parameters}
    assert params == {"breed": False, "count": False}
