"""New dashboard pages (Scam Shield, Birthdays) and the backend speed helpers."""
import asyncio

import pytest

from api import dash
from database import Database
from utils import dash_schema as S

CH = {"text": {"100"}, "category": set(), "voice": set()}
ROLES = {"500", "501"}


def v(mod, values, premium=False):
    return S.validate_values(S.BY_ID[mod], values, CH, ROLES, premium)


# ───────────────────────── schema: new modules ─────────────────────────

def test_every_module_getter_and_setter_exists_on_database():
    for m in S.MODULES:
        assert hasattr(Database, m["get"]), (m["id"], m["get"])
        assert hasattr(Database, m["set"]), (m["id"], m["set"])


def test_scam_shield_accepts_valid_values():
    ok, err = v("scamshield", {"enabled": True, "punish_action": "timeout",
                               "allowed_domains": ["example.com", " Example.com ", "docs.example.org"]})
    assert not err
    assert ok == {"enabled": True, "punish_action": "timeout",
                  "allowed_domains": ["example.com", "docs.example.org"]}   # de-duplicated, trimmed


def test_scam_shield_rejects_bad_values():
    for bad in ({"punish_action": "nuke"}, {"enabled": "yes"}, {"allowed_domains": "example.com"},
                {"allowed_domains": ["x" * 101]}, {"allowed_domains": [f"d{i}.com" for i in range(51)]}):
        assert v("scamshield", bad)[1], bad


def test_scam_shield_actions_match_the_bot():
    from modules import scam_shield
    offered = {o[0] for f in S.BY_ID["scamshield"]["fields"] if f["key"] == "punish_action" for o in f["options"]}
    assert offered == set(scam_shield.PUNISH_ACTIONS)


def test_birthdays_numbers_and_text():
    ok, err = v("birthdays", {"enabled": True, "channel_id": "100", "utc_offset_hours": "-5",
                              "announce_hour": 23, "message": "  Happy birthday {user}!  ", "ping_role_id": "500"})
    assert not err
    assert ok["utc_offset_hours"] == -5 and ok["announce_hour"] == 23
    assert ok["channel_id"] == 100 and ok["ping_role_id"] == 500
    assert ok["message"] == "Happy birthday {user}!"
    for bad in ({"utc_offset_hours": 15}, {"utc_offset_hours": -13}, {"announce_hour": 24},
                {"announce_hour": -1}, {"message": "x" * 501}, {"channel_id": "999"}, {"ping_role_id": "999"}):
        assert v("birthdays", bad)[1], bad


def test_birthday_role_is_a_premium_perk():
    assert v("birthdays", {"birthday_role_id": "500"}, premium=False)[1]
    assert v("birthdays", {"birthday_role_id": "500"}, premium=True) == ({"birthday_role_id": 500}, [])


def test_birthdays_only_writes_fields_the_store_accepts():
    from modules import birthday_store
    keys = {f["key"] for f in S.BY_ID["birthdays"]["fields"]}
    assert keys <= set(birthday_store._CONFIG_FIELDS)


def test_birthday_defaults_round_trip_through_reset():
    from modules import birthday_store
    defaults = birthday_store._defaults(1, None)
    clean, left = S.default_values(S.BY_ID["birthdays"], defaults)
    assert not left
    assert clean["enabled"] is False and clean["announce_hour"] == 9 and clean["utc_offset_hours"] == 0


# ───────────────────────── speed helpers ─────────────────────────

@pytest.mark.asyncio
async def test_once_runs_the_work_a_single_time_for_concurrent_callers():
    calls = []

    async def work():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "answer"

    results = await asyncio.gather(*[dash._once("k", work) for _ in range(8)])
    assert results == ["answer"] * 8
    assert len(calls) == 1
    assert not dash._inflight                      # nothing left behind


@pytest.mark.asyncio
async def test_once_does_not_cache_failures():
    state = {"n": 0}

    async def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError):
        await dash._once("f", flaky)
    assert await dash._once("f", flaky) == "ok"    # the next caller retries


@pytest.mark.asyncio
async def test_once_waiter_cancellation_does_not_kill_shared_work():
    async def work():
        await asyncio.sleep(0.05)
        return 1

    first = asyncio.ensure_future(dash._once("c", work))
    second = asyncio.ensure_future(dash._once("c", work))
    await asyncio.sleep(0.01)
    first.cancel()
    assert await second == 1


@pytest.mark.asyncio
async def test_http_session_is_reused_within_a_loop():
    a, b = dash._http(), dash._http()
    assert a is b
    await a.close()


def test_http_session_is_replaced_when_the_loop_changes():
    async def grab():
        return dash._http()

    s1 = asyncio.run(grab())
    s2 = asyncio.run(grab())                        # a different loop: must not reuse s1
    assert s1 is not s2


# ───────────────────────── anime feed page ─────────────────────────

def test_anime_feed_validation():
    ok, err = v("animefeed", {"enabled": True, "channel_id": "100", "ping_role_id": "500", "content_kinds": "donghua"})
    assert not err and ok == {"enabled": True, "channel_id": 100, "ping_role_id": 500, "content_kinds": "donghua"}
    for bad in ({"content_kinds": "everything"}, {"channel_id": "999"}, {"ping_role_id": "999"}, {"enabled": 1}):
        assert v("animefeed", bad)[1], bad


def test_anime_feed_kinds_match_the_bot():
    from modules import anime_feed
    offered = {o[0] for f in S.BY_ID["animefeed"]["fields"] if f["key"] == "content_kinds" for o in f["options"]}
    assert offered == set(anime_feed.KINDS)


def test_every_schema_icon_exists_in_the_dashboard_frontend():
    import re
    js = open("dashboard/assets/dash.js", encoding="utf-8").read()
    for m in S.MODULES:
        assert re.search(r'(^|\s)"?%s"?\s*:' % re.escape(m["icon"]), js), (m["id"], m["icon"])


@pytest.mark.asyncio
async def test_anime_feed_wrapper_round_trip(monkeypatch):
    from modules import anime_feed
    saved = {}

    async def fake_get(gid, clone_id=None):
        return saved.get("cfg") or anime_feed.FeedConfig(gid, clone_id)

    async def fake_save(cfg, updated_by=None):
        saved["cfg"] = cfg

    monkeypatch.setattr(anime_feed, "get_config", fake_get)
    monkeypatch.setattr(anime_feed, "save_config", fake_save)
    db = Database.__new__(Database)

    out = await db.set_anime_feed_config(1, None, channel_id=100, ping_role_id=500, content_kinds="anime", enabled=True)
    assert out == {"enabled": True, "channel_id": 100, "ping_role_id": 500, "content_kinds": "anime"}
    # enabled with no channel reads back as off, matching what save_config actually stores
    saved.clear()
    out = await db.set_anime_feed_config(1, None, enabled=True)
    assert out["enabled"] is False
    with pytest.raises(ValueError):
        await db.set_anime_feed_config(1, None, enabled_at="x")
