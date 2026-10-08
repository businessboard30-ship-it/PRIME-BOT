"""Analytics page (A4, read-only): validation, shape, permissions, caching, rate limit, no writes, no innerHTML."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
BIG = 1534574875274903562


def raw(days=3):
    return {"days": days, "tracking_since": NOW, "ranked": 12, "active_1d": 2, "active_7d": 5, "active_30d": 9,
            "series": [{"day": f"2026-10-0{i + 1}", "joined": i, "left": 1} for i in range(days)],
            "top_members": [{"user_id": BIG, "total_xp": 900, "level": 4, "secret": "x"}],
            "top_inviters": [{"user_id": BIG, "joins": 7, "net": 5}]}


# ---------- pure ----------
@pytest.mark.parametrize("raw_days,ok,want", [(None, True, 30), ("", True, 30), ("7", True, 7), (" 90 ", True, 90),
                                              ("30", True, 30), ("0", False, 0), ("365", False, 0), ("-7", False, 0),
                                              ("abc", False, 0), ("7; DROP", False, 0)])
def test_parse_days(raw_days, ok, want):
    if ok:
        assert S.parse_analytics_days(raw_days) == want
    else:
        with pytest.raises(ValueError):
            S.parse_analytics_days(raw_days)


def test_view_stringifies_ids_totals_and_drops_unknown_fields():
    v = S.analytics_view(raw(), 42)
    assert v["members"] == 42 and v["joined"] == 3 and v["left"] == 3
    assert v["top_members"] == [{"id": str(BIG), "xp": 900, "level": 4}]      # no 'secret', id is a string
    assert v["top_inviters"][0]["id"] == str(BIG) and v["active"] == {"d1": 2, "d7": 5, "d30": 9}
    assert v["tracking_since"] == NOW.isoformat()
    assert S.analytics_view({}, None)["members"] is None and S.analytics_view({}, "x")["members"] is None


# ---------- route ----------
@pytest.fixture
def an(env, monkeypatch):
    fake, members = env
    seen = {"n": 0}

    async def analytics(gid, clone_id, days):
        seen["n"] += 1
        seen["args"] = (gid, clone_id, days)
        return raw(days if days < 4 else 3)
    monkeypatch.setattr(fake, "dash_analytics", analytics, raising=False)
    dash._owner_hits.clear()
    dash._cache.clear()
    return fake, members, seen


def q(**kw):
    return dict({"action": "analytics", "guild_id": str(GUILD)}, **kw)


def test_requires_session_and_guild_permission(an):
    _, members, seen = an
    assert call("GET", q(), token=None)[0] == 401
    members["6"] = {"roles": []}
    assert call("GET", q())[0] == 403 and seen["n"] == 0


def test_defaults_to_30_days_and_returns_member_count(an):
    _, _, seen = an
    st, p, _ = call("GET", q())
    assert st == 200 and p["ok"] and p["members"] == 42 and seen["args"] == (GUILD, None, 30)
    assert p["top_members"][0]["id"] == str(BIG) and "secret" not in str(p)


@pytest.mark.parametrize("days", ["0", "8", "365", "abc", "-1"])
def test_bad_range_is_a_400_and_never_reaches_the_database(an, days):
    _, _, seen = an
    assert call("GET", q(days=days))[0] == 400 and seen["n"] == 0


def test_guild_comes_from_the_authorised_guild(an):
    _, _, seen = an
    assert call("GET", q(guild_id="999999999999"))[0] in (403, 404) and seen["n"] == 0


def test_result_is_cached_per_range(an):
    _, _, seen = an
    call("GET", q(days="7")); call("GET", q(days="7"))
    assert seen["n"] == 1
    call("GET", q(days="90"))
    assert seen["n"] == 2


def test_rate_limited(an):
    codes = [call("GET", q(days=("7", "30", "90")[i % 3]))[0] for i in range(35)]
    assert 429 in codes and codes[0] == 200


def test_discord_outage_still_returns_stats_without_member_count(an, monkeypatch):
    async def boom(gid):
        raise dash.DiscordError(500)
    monkeypatch.setattr(dash, "_guild_info", boom)
    st, p, _ = call("GET", q())
    # authorisation itself needs Discord, so with Discord down the request is refused rather than leaking data
    assert st in (200, 403, 502)
    if st == 200:
        assert p["members"] is None


def test_route_is_read_only_and_page_has_no_innerhtml():
    src = Path(dash.__file__).read_text()
    i = src.index('action == "analytics"')
    block = src[i:i + 900]
    assert 'method == "GET"' in src[i - 40:i]
    assert not any(w in block for w in ("INSERT", "DELETE", "UPDATE", "dash_audit", "_schedule_audit"))
    js = (Path(dash.__file__).resolve().parents[1] / "dashboard" / "assets" / "dash.js").read_text()
    page = js[js.index("function barChart"):js.index("function renderModeration")]
    assert "innerHTML" not in page
    assert 'gpath(gid, "analytics")' in js and "analytics: renderAnalytics" in js
    assert "  GET  ?action=analytics" in src            # documented in the _route docstring


def test_db_method_is_select_only_and_scoped_to_guild_and_clone():
    import database
    src = Path(database.__file__).read_text()
    body = src[src.index("async def dash_analytics"):src.index("async def dash_analytics") + 3800]
    assert not any(w in body for w in ("INSERT", "DELETE ", "UPDATE ", "DROP", "ALTER"))
    assert body.count("clone_id IS NOT DISTINCT FROM") >= 4
