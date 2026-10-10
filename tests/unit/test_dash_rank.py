"""Phase A: My rank. Own position only, session-keyed, parity with the bot's global rank."""
from pathlib import Path

import pytest

from api import dash, dash_member
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401

SERVERS = [
    {"guild_id": 111, "total_xp": 500, "level": 3, "guild_name": "Hub", "rank": 2, "players": 9},
    {"guild_id": 222, "total_xp": 90, "level": 1, "guild_name": "Small", "rank": 7, "players": 8},
    {"guild_id": 333, "total_xp": 80, "level": 1, "guild_name": "Top", "rank": 1, "players": 3},
]


@pytest.fixture
def rank(mem, monkeypatch):
    fake, _ = mem
    log = []

    async def get_global_xp_rank(uid):
        log.append(("global", uid))
        return {"total_xp": 670, "rank": 4, "total_players": 20}

    async def member_servers(uid, all_bots=False):
        log.append(("servers", uid))
        return [dict(s) for s in SERVERS]
    monkeypatch.setattr(fake, "get_global_xp_rank", get_global_xp_rank, raising=False)
    monkeypatch.setattr(fake, "member_servers", member_servers, raising=False)
    return log


def test_rank_is_keyed_to_session_and_ignores_client_ids(rank):
    st, p, _ = call("GET", {"action": "member_rank", "user_id": "999"})
    assert st == 200 and rank == [("global", 6), ("servers", "6")]
    assert p["global"]["rank"] == 4 and p["global"]["players"] == 20 and p["global"]["total_xp"] == 670


def test_rank_matches_bot_global_rank_and_level_maths(rank):
    from modules.leveling import xp_progress
    _, p, _ = call("GET", {"action": "member_rank"})
    pr = xp_progress(670)
    assert p["global"]["level"] == pr["level"] and p["global"]["xp_for_next"] == pr["xp_needed_for_next_level"]


def test_best_and_worst_server(rank):
    _, p, _ = call("GET", {"action": "member_rank"})
    assert p["best"]["name"] == "Top" and p["best"]["rank"] == 1
    assert p["worst"]["name"] == "Small" and p["worst"]["rank"] == 7
    assert all(isinstance(s["guild_id"], str) for s in p["servers"])


def test_empty_state_when_no_xp(mem, monkeypatch):
    fake, _ = mem

    async def none(uid):
        return None

    async def empty(uid, all_bots=False):
        return []
    monkeypatch.setattr(fake, "get_global_xp_rank", none, raising=False)
    monkeypatch.setattr(fake, "member_servers", empty, raising=False)
    st, p, _ = call("GET", {"action": "member_rank"})
    p.pop("ok", None)
    assert st == 200 and p == {"global": None, "servers": [], "best": None, "worst": None}


def test_no_other_user_is_named(rank):
    _, p, _ = call("GET", {"action": "member_rank"})
    assert "user_id" not in str(p) and "username" not in str(p)


def test_requires_session(rank):
    assert call("GET", {"action": "member_rank"}, token=None)[0] == 401


def test_rate_limited(rank):
    dash._owner_hits.clear()
    codes = [call("GET", {"action": "member_rank"})[0] for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_route_is_unique_and_documented_and_front_end_is_safe():
    assert "member_rank" not in dash._owner_routes() and "member_rank" not in dash_member.WRITES
    assert "member_rank" in Path(dash.__file__).read_text()
    js = Path("dashboard/assets/dash.js").read_text()
    assert ".innerHTML" not in js and "innerHTML =" not in js and 'api("member_rank")' in js
