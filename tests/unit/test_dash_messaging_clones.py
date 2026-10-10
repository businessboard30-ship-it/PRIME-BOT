"""Part D on clone-only servers: people who share only a clone server can search, request and message.
Every Discord call for a clone server runs AS that clone (its token, its own server list and cache); everything fails closed."""
import json

import pytest

from api import dash, dash_msg
from tests.unit.test_dash_messaging import A, B, C, D, G1, get, post, msg  # noqa: F401  (msg is the shared fixture)
from tests.unit.test_dash_api import call, env  # noqa: F401

GC, GC2, GX = "900300000000000003", "900400000000000004", "900500000000000005"
TOKENS = {None: "MAIN-TOKEN", 7: "tok-seven", 8: "tok-eight", 9: ""}      # 9: active but its token is gone
NAMES = {A: "Alice", B: "Bob", C: "Cara", D: "Dan"}


@pytest.fixture
def cl(msg, monkeypatch):
    """Main bot is in G1; clone 7 is in GC (and GC2); clone 8 is INACTIVE in GX; clone 9 is active but its token is gone."""
    st = msg["st"]
    s = {"calls": [], "down": set(), "rows": {},
         "members": {"MAIN-TOKEN": {G1: {A, B, C}}, "tok-seven": {GC: {A, B}, GC2: {A, D}}, "tok-eight": {GX: {A, B}}, "tok-nine": {GX: {A, B}}},
         "active": {7: True, 8: False, 9: True}}

    async def guilds(uid, limit=25):
        return [dict(r) for r in s["rows"].get(str(uid), [])][:limit]

    def row(gid, clone=None, name=None):
        return {"guild_id": gid, "name": "Server " + gid, "clone_id": clone, "bot_username": name}

    async def clone_row(cid):
        if not s["active"].get(int(cid)):
            return None
        return {"clone_id": int(cid), "status": "active", "tok": TOKENS.get(int(cid), "")}

    def clone_token(r):
        return r["tok"]

    async def bot_get(path):
        token = dash._BOT.get()[1] or "MAIN-TOKEN"
        s["calls"].append((token, path))
        if token in s["down"]:
            raise dash.DiscordError(500)
        guilds_here = s["members"].get(token, {})
        if path.startswith("/users/@me/guilds"):
            return [{"id": g} for g in guilds_here]
        if path.startswith("/guilds/") and "/members/search" in path:
            gid = path.split("/")[2]
            term = path.split("query=")[1].split("&")[0].lower()
            if gid not in guilds_here:
                raise dash.DiscordError(404)
            return [{"user": {"id": u, "username": NAMES[u], "global_name": NAMES[u]}} for u in guilds_here[gid] if NAMES[u].lower().startswith(term)]
        if path.startswith("/guilds/") and "/members/" in path:
            gid, who = path.split("/")[2], path.rsplit("/", 1)[1]
            if who not in guilds_here.get(gid, ()):
                raise dash.DiscordError(404)
            return {"user": {"id": who}}
        if path.startswith("/users/"):
            who = path.rsplit("/", 1)[1]
            return {"id": who, "username": NAMES.get(who, "x"), "global_name": NAMES.get(who)}
        raise dash.DiscordError(404)
    monkeypatch.setattr(st, "msg_guild_ids", guilds, raising=False)
    monkeypatch.setattr(dash.db, "msg_guild_ids", guilds, raising=False)
    monkeypatch.setattr(dash, "_clone_row", clone_row)
    monkeypatch.setattr(dash, "_clone_token", clone_token)
    monkeypatch.setattr(dash, "_bot_get", bot_get)
    s["row"] = row
    dash._cache.clear()
    return s


def only_clone(cl, who=(A, B)):
    for u in who:
        cl["rows"][u] = [cl["row"](GC, 7, "Pixel")]


def tokens_used(cl):
    return {t for t, _ in cl["calls"]}


# ---------- clone-only server works ----------
def test_clone_only_server_allows_request_accept_and_message(cl):
    only_clone(cl)
    assert post("A", "friend_request", other_id=B)[0] == 200
    assert post("B", "friend_respond", other_id=A, accept=True)[0] == 200
    st, p, _ = post("A", "message_send", other_id=B, body="hello from a clone server")
    assert st == 200
    assert get("B", "messages_thread", other_id=A)[1]["messages"][0]["body"] == "hello from a clone server"
    member_calls = [(t, path) for t, path in cl["calls"] if "/members/" in path]
    assert member_calls and all(t == "tok-seven" for t, _ in member_calls)        # checked AS the clone, never the main bot


def test_a_request_to_a_clone_only_neighbour_is_actually_delivered(cl):
    only_clone(cl)
    post("A", "friend_request", other_id=B)
    assert [x["user_id"] for x in get("B", "friends_list")[1]["incoming"]] == [A]


def test_search_in_a_clone_server_runs_as_that_clone_and_finds_people(cl):
    only_clone(cl)
    st, p, _ = get("A", "friends_search", guild_id=GC, clone="7", query="bo")
    assert st == 200 and [r["user_id"] for r in p["results"]] == [B]
    assert tokens_used(cl) == {"tok-seven"}


def test_friends_list_shows_clone_servers_with_the_bot_name(cl):
    cl["rows"][A] = [cl["row"](G1), cl["row"](GC, 7, "Pixel")]
    servers = get("A", "friends_list")[1]["servers"]
    assert servers[0] == {"guild_id": G1, "name": "Server " + G1}                  # main rows keep their old shape
    assert servers[1] == {"guild_id": GC, "name": "Server " + GC, "clone": 7, "bot": "Pixel"}


# ---------- main bot unchanged ----------
def test_main_bot_only_behaviour_is_unchanged(cl):
    cl["rows"][A] = [cl["row"](G1)]
    cl["rows"][B] = [cl["row"](G1)]
    st, p, _ = get("A", "friends_search", guild_id=G1, query="bo")
    assert st == 200 and [r["user_id"] for r in p["results"]] == [B] and tokens_used(cl) == {"MAIN-TOKEN"}
    assert post("A", "friend_request", other_id=B)[0] == 200
    assert [x["user_id"] for x in get("B", "friends_list")[1]["incoming"]] == [A]


def test_a_main_server_cannot_be_searched_as_a_clone_or_the_reverse(cl):
    cl["rows"][A] = [cl["row"](G1), cl["row"](GC, 7, "Pixel")]
    assert get("A", "friends_search", guild_id=G1, clone="7", query="bo")[0] == 403       # G1 is a main-bot server
    assert get("A", "friends_search", guild_id=GC, query="bo")[0] == 403                  # GC is a clone server
    assert get("A", "friends_search", guild_id=GC, clone="8", query="bo")[0] == 403       # other clone
    assert not [c for c in cl["calls"] if "/members/search" in c[1]]


# ---------- fail closed ----------
def test_inactive_clone_is_refused_everywhere(cl):
    cl["rows"][A] = cl["rows"][B] = [cl["row"](GX, 8, "Old")]
    assert get("A", "friends_search", guild_id=GX, clone="8", query="bo")[0] == 403
    assert post("A", "friend_request", other_id=B)[0] == 200                              # same generic reply...
    assert get("B", "friends_list")[1]["incoming"] == []                                  # ...but nothing was delivered
    assert get("A", "friends_list")[1]["servers"] == []
    assert not cl["calls"] or all("tok-eight" not in t for t, _ in cl["calls"])


def test_active_clone_with_no_token_is_refused(cl):
    cl["rows"][A] = cl["rows"][B] = [cl["row"](GX, 9, "NoTok")]
    assert get("A", "friends_search", guild_id=GX, clone="9", query="bo")[0] == 403
    post("A", "friend_request", other_id=B)
    assert get("B", "friends_list")[1]["incoming"] == []


@pytest.mark.parametrize("missing", [A, B])
def test_discord_404_for_either_person_means_not_shared(cl, missing):
    only_clone(cl)
    cl["members"]["tok-seven"][GC].discard(missing)
    post("A", "friend_request", other_id=B)
    assert get("B", "friends_list")[1]["incoming"] == []


def test_discord_error_fails_closed_and_is_not_cached(cl):
    only_clone(cl)
    cl["down"].add("tok-seven")
    post("A", "friend_request", other_id=B)
    assert get("B", "friends_list")[1]["incoming"] == []
    assert get("A", "friends_search", guild_id=GC, clone="7", query="bo")[0] == 502
    cl["down"].clear()
    dash._owner_hits.clear()
    assert get("A", "friends_search", guild_id=GC, clone="7", query="bo")[0] == 200


def test_a_broken_clone_does_not_hide_a_working_main_server(cl):
    cl["rows"][A] = [cl["row"](GC, 7, "Pixel"), cl["row"](G1)]
    cl["rows"][B] = [cl["row"](G1)]
    cl["down"].add("tok-seven")
    post("A", "friend_request", other_id=B)
    assert [x["user_id"] for x in get("B", "friends_list")[1]["incoming"]] == [A]         # shared through the main bot
    assert [s["guild_id"] for s in get("A", "friends_list")[1]["servers"]] == [G1]


def test_search_for_a_server_the_user_is_not_in_as_that_bot_is_refused(cl):
    cl["rows"][A] = [cl["row"](GC, 7, "Pixel")]
    cl["members"]["tok-seven"][GC].discard(A)                                            # XP row is stale: Discord says not a member
    assert get("A", "friends_search", guild_id=GC, clone="7", query="bo")[0] == 403
    assert get("A", "friends_search", guild_id=GC2, clone="7", query="bo")[0] == 403      # no XP row there at all
    assert not [c for c in cl["calls"] if "/members/search" in c[1]]


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "99999999999", "7 ", "7;8"])
def test_bad_clone_values_are_refused_before_discord(cl, bad):
    only_clone(cl)
    st, p, _ = get("A", "friends_search", guild_id=GC, clone=bad, query="bo")
    assert st in (403, 422) and cl["calls"] == []


# ---------- isolation, caches, context ----------
def test_bot_context_is_reset_after_clone_calls(cl):
    only_clone(cl)
    get("A", "friends_search", guild_id=GC, clone="7", query="bo")
    post("A", "friend_request", other_id=B)
    assert dash._BOT.get() == (None, None)
    cl["calls"].clear()
    cl["rows"][A] = cl["rows"][B] = [cl["row"](G1)]
    get("A", "friends_search", guild_id=G1, query="bo")
    assert tokens_used(cl) == {"MAIN-TOKEN"}                                              # a later call never runs as the clone


def test_context_is_reset_even_when_discord_raises(cl):
    only_clone(cl)
    cl["down"].add("tok-seven")
    get("A", "friends_search", guild_id=GC, clone="7", query="bo")
    post("A", "friend_request", other_id=B)
    assert dash._BOT.get() == (None, None)


def test_server_lists_are_cached_per_bot(cl):
    cl["rows"][A] = [cl["row"](G1), cl["row"](GC, 7, "Pixel"), cl["row"](GC2, 7, "Pixel")]
    get("A", "friends_list")
    keys = [k for k in dash._cache if k[1] == "botguilds"]
    assert {k[0] for k in keys} == {None, 7}                                              # one entry per bot, never shared
    before = len([c for c in cl["calls"] if c[1].startswith("/users/@me/guilds")])
    get("A", "friends_list")
    assert len([c for c in cl["calls"] if c[1].startswith("/users/@me/guilds")]) == before    # second call served from cache


def test_a_pair_result_is_not_leaked_across_pairs(cl):
    only_clone(cl)
    cl["rows"][C] = [cl["row"](G1)]
    post("A", "friend_request", other_id=B)
    post("A", "friend_request", other_id=C)                                               # A and C share nothing
    assert [x["user_id"] for x in get("C", "friends_list")[1]["incoming"]] == []
    assert [x["user_id"] for x in get("B", "friends_list")[1]["incoming"]] == [A]


def test_candidate_cap_of_ten_member_checks_holds_across_bots(cl):
    many = [str(900600000000000000 + i) for i in range(30)]
    cl["rows"][A] = [cl["row"](g, 7, "Pixel") for g in many]
    cl["rows"][B] = [cl["row"](g, 7, "Pixel") for g in many]
    cl["members"]["tok-seven"] = {g: set() for g in many}                                 # never shared
    post("A", "friend_request", other_id=B)
    checks = [c for c in cl["calls"] if "/members/" in c[1] and "/search" not in c[1]]
    assert 0 < len(checks) <= 10


def test_no_ids_or_tokens_leak_in_server_rows_or_errors(cl):
    cl["rows"][A] = [cl["row"](G1), cl["row"](GC, 7, "Pixel")]
    only = get("A", "friends_list")[1]["servers"]
    errs = [get("A", "friends_search", guild_id=GX, clone="8", query="bo")[1], get("A", "friends_search", guild_id=GC, clone="abc", query="bo")[1]]
    blob = json.dumps([only, errs])
    for secret in ("tok-seven", "tok-eight", "MAIN-TOKEN", B, C, D):
        assert secret not in blob
    assert set(only[1]) == {"guild_id", "name", "clone", "bot"}


def test_ui_uses_clone_not_clone_id_and_shows_the_bot_name():
    from pathlib import Path
    js = Path("dashboard/assets/dash.js").read_text()
    i = js.index('api("friends_search"')
    block = js[i - 400:i + 80]
    assert "sp.clone = pv[1]" in block and "clone_id" not in block
    assert 'g.name + " \\u00b7 " + g.bot' in js
