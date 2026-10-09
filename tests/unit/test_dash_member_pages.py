"""Member pages (#/me): servers, preferences, purchases. Everything is keyed to the SESSION user."""
import re
from pathlib import Path

import pytest

from api import dash, dash_member
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401

SRC = Path(dash_member.__file__).read_text()
ROW = {"guild_id": 111, "total_xp": 500, "level": 3, "guild_name": "Hub", "rank": 2, "players": 9,
       "balance": 50, "currency_symbol": "$", "currency_name": "Coins", "ping_optout": False}


@pytest.fixture
def pages(mem, monkeypatch):
    fake, seen = mem
    log = []

    async def member_servers(uid, all_bots=False):
        log.append(("servers", uid))
        return [dict(ROW)]

    async def member_purchases(uid, limit=50):
        log.append(("buys", uid))
        return [{"amount": 5.0, "status": "success", "payment_type": "premium", "provider": "paystack", "created_date": None,
                 "paystack_reference": "SECRET"}]

    async def get_user_language(uid, clone=0):
        return "en"

    async def get_user_currency(uid):
        return None

    async def rec(name):
        pass
    for n in ("set_user_language", "set_user_currency", "member_level_ping_set"):
        def mk(n):
            async def f(*a, **k):
                log.append((n, a))
            return f
        monkeypatch.setattr(fake, n, mk(n), raising=False)
    for n, f in (("member_servers", member_servers), ("member_purchases", member_purchases),
                 ("get_user_language", get_user_language), ("get_user_currency", get_user_currency)):
        monkeypatch.setattr(fake, n, f, raising=False)

    from modules import ai_prefs

    async def get_prefs(uid):
        return ("genz", "auto")

    async def set_character(uid, c):
        log.append(("char", uid, c))
        return True
    monkeypatch.setattr(ai_prefs, "get_prefs", get_prefs)
    async def set_voice_mode(uid, m):
        log.append(("voice", uid, m))
        return True
    monkeypatch.setattr(ai_prefs, "set_character", set_character)
    monkeypatch.setattr(ai_prefs, "set_voice_mode", set_voice_mode)
    return log


def test_servers_view_is_keyed_to_session_and_uses_string_ids(pages):
    st, p, _ = call("GET", {"action": "member_servers", "user_id": "999"})
    assert st == 200 and pages == [("servers", "6")]
    g = p["servers"][0]
    assert g["guild_id"] == "111" and g["level"] == 3 and g["rank"] == 2 and g["coins"] == 50
    assert 0 <= g["xp_in_level"] <= g["xp_for_next"]


def test_purchases_never_return_gateway_reference(pages):
    st, p, _ = call("GET", {"action": "member_purchases"})
    assert st == 200 and "SECRET" not in str(p) and "reference" not in str(p) and pages == [("buys", "6")]


def test_prefs_lists_allowed_values(pages):
    st, p, _ = call("GET", {"action": "member_prefs"})
    assert st == 200 and "en" in p["languages"] and "USD" in p["currencies"] and p["character"] == "genz"


@pytest.mark.parametrize("kind,value", [("language", "en"), ("currency", "usd"), ("character", "genz"), ("voice", "off")])
def test_pref_set_accepts_allowlisted_values(pages, kind, value):
    st, _, _ = call("POST", body={"action": "member_pref_set", "kind": kind, "value": value, "user_id": "999"})
    assert st == 200 and pages and all("999" not in str(x) for x in pages)       # body user id ignored


@pytest.mark.parametrize("kind,value", [("language", "xx"), ("currency", "ZZZ"), ("character", "evil"), ("voice", "loud"),
                                         ("nope", "x"), ("language", None), ("level_ping", "yes")])
def test_pref_set_rejects_everything_else(pages, kind, value):
    st, _, _ = call("POST", body={"action": "member_pref_set", "kind": kind, "value": value, "guild_id": "111"})
    assert st == 400 and pages == []


def test_level_ping_only_for_servers_where_user_has_xp(pages):
    assert call("POST", body={"action": "member_pref_set", "kind": "level_ping", "guild_id": "222", "value": True})[0] == 404
    st, _, _ = call("POST", body={"action": "member_pref_set", "kind": "level_ping", "guild_id": "111", "value": True})
    assert st == 200 and ("member_level_ping_set", (111, 6, True)) in pages


def test_pref_set_requires_session_and_is_rate_limited(pages):
    assert call("POST", body={"action": "member_pref_set", "kind": "voice", "value": "off"}, token=None)[0] == 401
    dash._owner_hits.clear()
    codes = [call("POST", body={"action": "member_pref_set", "kind": "voice", "value": "off"})[0] for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_write_routes_do_not_collide_and_take_no_uid_from_body():
    assert not (set(dash_member.WRITES) & (set(dash._owner_writes()) | set(dash_member.ROUTES)))
    assert not re.search(r'body\.get\(\s*["\'](user_id|uid)', SRC)


def test_frontend_never_uses_innerhtml_for_member_data():
    js = Path("dashboard/assets/dash.js").read_text()
    assert "innerHTML" not in js[js.index("function renderMe"):js.index("function renderOwner")]
