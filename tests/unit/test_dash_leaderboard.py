"""Phase B: web global leaderboard. Parity with the bot, opt-out hides names, no ids leak, own row pinned."""
import inspect
from pathlib import Path

import pytest

import database
from api import dash, dash_member
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401

ROWS = [{"user_id": 111, "total_xp": 5000}, {"user_id": 6, "total_xp": 900}, {"user_id": 333, "total_xp": 400}]


@pytest.fixture
def board(mem, monkeypatch):
    fake, _ = mem
    st = {"pages": [], "visible": True, "profiles": {"111": {"name": "Ann", "avatar": "https://cdn.discordapp.com/a.png"},
                                                     "6": {"name": "Me", "avatar": None}}, "audit": []}

    async def page(limit=10, offset=0):
        st["pages"].append((limit, offset))
        return ROWS[offset:offset + limit], len(ROWS)

    async def profiles(ids):
        return {i: v for i, v in st["profiles"].items() if i in [str(x) for x in ids]}

    async def my_rank(uid):
        return {"total_xp": 900, "rank": 2, "total_players": 3} if uid == 6 else None

    async def vis_get(uid):
        return st["visible"]

    async def vis_set(uid, v):
        st["visible"] = v
        st["last_set"] = (uid, v)
    for n, f in (("get_global_xp_leaderboard_page", page), ("board_profiles", profiles), ("get_global_xp_rank", my_rank),
                 ("board_visible_get", vis_get), ("board_visible_set", vis_set)):
        monkeypatch.setattr(fake, n, f, raising=False)
    from modules import admin_controls

    async def rec(admin_id, action, gid, details):
        st["audit"].append((admin_id, action, details))
    monkeypatch.setattr(admin_controls, "record_audit", rec)
    dash._cache.clear()
    return st


def test_entries_parity_with_bot_and_you_flag(board):
    from modules.leveling import compute_level
    st, p, _ = call("GET", {"action": "member_leaderboard"})
    assert st == 200 and board["pages"] == [(10, 0)]
    assert [e["rank"] for e in p["entries"]] == [1, 2, 3]
    assert [e["xp"] for e in p["entries"]] == [5000, 900, 400]
    assert p["entries"][0]["level"] == compute_level(5000)
    assert [e["you"] for e in p["entries"]] == [False, True, False]
    assert p["me"]["rank"] == 2 and p["total"] == 3 and p["pages"] == 1


def test_hidden_players_have_no_name_avatar_or_id(board):
    _, p, _ = call("GET", {"action": "member_leaderboard"})
    third = p["entries"][2]
    assert third["name"] == "Hidden player" and third["avatar"] is None and third["hidden"] is True
    assert p["entries"][0]["name"] == "Ann"
    blob = str(p)
    assert "333" not in blob and "111" not in blob and "user_id" not in blob and "_uid" not in blob


def test_own_row_pinned_even_when_outside_the_page(board, monkeypatch):
    async def far(uid):
        return {"total_xp": 10, "rank": 4000, "total_players": 9000}
    monkeypatch.setattr(dash.db, "get_global_xp_rank", far, raising=False)
    _, p, _ = call("GET", {"action": "member_leaderboard"})
    assert p["me"]["rank"] == 4000 and p["me"]["players"] == 9000


@pytest.mark.parametrize("page,ok", [("0", True), ("49", True), ("50", False), ("-1", False), ("abc", False), ("999999", False)])
def test_pagination_bounds(board, page, ok):
    st, _, _ = call("GET", {"action": "member_leaderboard", "page": page})
    assert (st == 200) is ok
    if not ok:
        assert board["pages"] == []


def test_page_offset_uses_bot_page_size(board):
    call("GET", {"action": "member_leaderboard", "page": "3"})
    assert board["pages"] == [(10, 30)]


def test_hot_pages_are_cached_but_you_flag_is_per_viewer(board):
    call("GET", {"action": "member_leaderboard"})
    call("GET", {"action": "member_leaderboard"})
    assert len(board["pages"]) == 1


def test_client_ids_ignored_and_session_required(board):
    _, p, _ = call("GET", {"action": "member_leaderboard", "user_id": "333"})
    assert [e["you"] for e in p["entries"]] == [False, True, False]
    assert call("GET", {"action": "member_leaderboard"}, token=None)[0] == 401


def test_rate_limited(board):
    dash._owner_hits.clear()
    codes = [call("GET", {"action": "member_leaderboard"})[0] for _ in range(61)]
    assert codes[60] == 429 and codes[0] == 200


def test_opt_out_sets_own_flag_audits_and_clears_cache(board):
    call("GET", {"action": "member_leaderboard"})
    st, _, _ = call("POST", body={"action": "member_board_pref", "show": False, "user_id": "999"})
    assert st == 200 and board["last_set"] == ("6", False) and board["audit"] == [(6, "member_board_pref", "show=False")]
    call("GET", {"action": "member_leaderboard"})
    assert len(board["pages"]) == 2           # cache was dropped


@pytest.mark.parametrize("v", ["yes", 1, None])
def test_opt_out_rejects_non_bool(board, v):
    assert call("POST", body={"action": "member_board_pref", "show": v})[0] == 400


def test_opt_out_requires_session(board):
    assert call("POST", body={"action": "member_board_pref", "show": True}, token=None)[0] == 401


def test_sql_only_names_opted_in_signed_in_members():
    src = inspect.getsource(database.Database.board_profiles)
    assert "show_on_board = TRUE" in src and "display_name IS NOT NULL" in src
    assert "avatar_url.startswith" not in src
    t = inspect.getsource(database.Database.dash_web_user_touch)
    assert "https://cdn.discordapp.com/" in t        # only Discord CDN avatars are stored


def test_actions_unique_and_front_end_safe():
    assert "member_leaderboard" not in dash._owner_routes() and "member_board_pref" not in dash._owner_writes()
    js = Path("dashboard/assets/dash.js").read_text()
    assert ".innerHTML" not in js and "innerHTML =" not in js and 'api("member_leaderboard"' in js
    assert "member_leaderboard" in Path(dash.__file__).read_text()
