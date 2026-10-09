"""Phase C: My clan. Own clan only, read-only, session-keyed, no ids leaked."""
from pathlib import Path

import pytest

from api import dash, dash_member
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401

SERVERS = [
    {"guild_id": 111, "total_xp": 5000, "level": 12, "guild_name": "Hub", "rank": 2, "players": 9},
    {"guild_id": 222, "total_xp": 90, "level": 1, "guild_name": "Small", "rank": 7, "players": 8},
]
SEATS = {
    111: [{"seat_rank": 1, "clan_slug": "Wolves", "user_id": 5551234567890123},
          {"seat_rank": 2, "clan_slug": "Knights", "user_id": 6},
          {"seat_rank": 3, "clan_slug": "Dragons", "user_id": 5559999999999999},
          {"seat_rank": 4, "clan_slug": "Monsters", "user_id": None},
          {"seat_rank": 5, "clan_slug": "Deviors", "user_id": None}],
    222: [],
}


@pytest.fixture
def clans(mem, monkeypatch):
    fake, _ = mem
    log = []

    async def member_servers(uid):
        log.append(("servers", uid))
        return [dict(s) for s in SERVERS]

    async def card(gid, uid, clone_id=None):
        log.append(("card", gid, uid, clone_id))
        return "clan_white_wolves.png" if gid == 111 else None

    async def chief(gid, uid, clone_id=None):
        return {"seat_rank": 2, "clan_slug": "Knights", "user_id": uid} if gid == 111 else None

    async def seats(gid, clone_id=None):
        return [dict(x) for x in SEATS[gid]]

    async def profiles(ids):
        return {"5551234567890123": {"name": "Shown Sam", "avatar": None}}      # the other holder opted out

    async def count(gid, clan_card, clone_id=None):
        return 42
    for name, fn in [("member_servers", member_servers), ("get_clan_card_if_assigned", card),
                     ("get_chief_seat_for_user", chief), ("get_clan_seats", seats),
                     ("board_profiles", profiles), ("get_clan_member_count", count)]:
        monkeypatch.setattr(fake, name, fn, raising=False)
    return log


def test_keyed_to_session_and_main_bot_only(clans):
    st, p, _ = call("GET", {"action": "member_clans", "user_id": "999"})
    assert st == 200 and ("servers", "6") in clans
    assert all(c[3] is None for c in clans if c[0] == "card") and all(c[2] == 6 for c in clans if c[0] == "card")


def test_own_clan_chief_seat_and_member_count(clans):
    _, p, _ = call("GET", {"action": "member_clans"})
    hub = p["servers"][0]
    assert hub["clan"]["members"] == 42 and hub["chief"] == {"seat": 2, "clan": "Knights"}
    assert hub["guild_id"] == "111" and hub["needs"] == {"rank": 5, "level": 3, "rank_ok": True, "level_ok": True}
    assert p["servers"][1]["clan"] is None and p["servers"][1]["chief"] is None
    assert p["servers"][1]["needs"]["rank_ok"] is False and p["servers"][1]["needs"]["level_ok"] is False


def test_seat_holders_follow_leaderboard_privacy_and_no_ids(clans):
    _, p, _ = call("GET", {"action": "member_clans"})
    seats = p["servers"][0]["seats"]
    assert [s["holder"] for s in seats] == ["Shown Sam", seats[1]["holder"], "Hidden player", None, None]
    assert seats[1]["you"] is True and seats[0]["you"] is False
    assert [s["filled"] for s in seats] == [True, True, True, False, False]
    blob = str(p)
    assert "5551234567890123" not in blob and "5559999999999999" not in blob and "user_id" not in blob


def test_empty_state_when_no_xp(mem, monkeypatch):
    fake, _ = mem

    async def empty(uid):
        return []
    monkeypatch.setattr(fake, "member_servers", empty, raising=False)
    st, p, _ = call("GET", {"action": "member_clans"})
    p.pop("ok", None)
    assert st == 200 and p == {"servers": []}


def test_requires_session(clans):
    assert call("GET", {"action": "member_clans"}, token=None)[0] == 401


def test_rate_limited(clans):
    dash._owner_hits.clear()
    codes = [call("GET", {"action": "member_clans"})[0] for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_capped_server_fanout(mem, monkeypatch):
    fake, _ = mem

    async def many(uid):
        return [{"guild_id": 1000 + i, "total_xp": 10, "level": 1, "guild_name": f"S{i}", "rank": 1, "players": 1} for i in range(40)]

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return []
    monkeypatch.setattr(fake, "member_servers", many, raising=False)
    monkeypatch.setattr(fake, "get_clan_card_if_assigned", none, raising=False)
    monkeypatch.setattr(fake, "get_chief_seat_for_user", none, raising=False)
    monkeypatch.setattr(fake, "get_clan_seats", empty, raising=False)
    _, p, _ = call("GET", {"action": "member_clans"})
    assert len(p["servers"]) == dash_member.CLAN_MAX_SERVERS


def test_read_only_no_write_route_and_front_end_is_safe():
    assert "member_clans" in dash_member.ROUTES and "member_clans" not in dash_member.WRITES
    assert "member_clans" not in dash._owner_routes()
    assert "member_clans" in Path(dash.__file__).read_text()
    for w in dash_member.WRITES:
        assert "clan" not in w
    js = Path("dashboard/assets/dash.js").read_text()
    assert ".innerHTML" not in js and "innerHTML =" not in js and 'api("member_clans")' in js
    assert '["clan", "flag", "My clan"]' in js and "clan: meClan" in js and '["clan", "flag", "My clan", ' in js


def test_parity_with_bot_chief_rules(clans, monkeypatch):
    import config
    from modules.clan_cards import CLAN_CARDS
    assert dash_member.CHIEF_SEATS == len(CLAN_CARDS) == 5
    monkeypatch.setattr(config, "CHIEF_MIN_LEVEL", 20)
    _, p, _ = call("GET", {"action": "member_clans"})
    assert p["servers"][0]["needs"]["level"] == 20 and p["servers"][0]["needs"]["level_ok"] is False
