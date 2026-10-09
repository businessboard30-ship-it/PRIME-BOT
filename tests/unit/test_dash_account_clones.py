"""Account pages include servers/payments on clone bots, labelled with the bot; main-only pages stay main-only."""
import inspect

from api import dash_member
from database import Database
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401


def row(**kw):
    base = {"guild_id": 10, "guild_name": "Main HQ", "total_xp": 500, "rank": 2, "players": 9, "balance": None,
            "clone_id": None, "bot_username": None, "ping_optout": False}
    base.update(kw)
    return base


def test_server_view_labels_the_bot_and_stringifies_ids():
    v = dash_member.server_view(row(guild_id=2 ** 60, clone_id=7, bot_username="Cloney"))
    assert v["clone_id"] == "7" and v["bot"] == "Cloney" and v["guild_id"] == str(2 ** 60)
    m = dash_member.server_view(row())
    assert m["clone_id"] is None and m["bot"] is None


def test_server_view_long_or_missing_bot_name_is_safe():
    assert len(dash_member.server_view(row(clone_id=1, bot_username="x" * 200))["bot"]) == 40
    assert dash_member.server_view(row(clone_id=1, bot_username=None))["bot"] == "Custom bot"


def test_purchase_view_names_the_bot_only_for_clone_payments():
    base = {"amount": 4, "status": "paid", "payment_type": "premium", "provider": "gumroad", "created_date": None}
    assert dash_member.purchase_view({**base, "bot_username": "Cloney"})["bot"] == "Cloney"
    assert dash_member.purchase_view(base)["bot"] is None


def test_pages_ask_for_all_bots_but_clans_stay_main_only(mem):
    fake, seen = mem
    calls = []

    async def ms(uid, all_bots=False):
        calls.append(all_bots)
        return [row(), row(guild_id=11, guild_name="Clone Hall", clone_id=7, bot_username="Cloney", total_xp=900)]
    fake.member_servers = ms
    st, p, _ = call("GET", {"action": "member_servers"})
    assert st == 200 and [s["bot"] for s in p["servers"]] == [None, "Cloney"] and calls == [True]
    st, p, _ = call("GET", {"action": "member_rank"})
    assert st == 200 and calls[-1] is True
    src = inspect.getsource(dash_member.member_clans)
    assert "all_bots" not in src                              # clan lookups are main-bot only


def test_level_ping_can_be_set_for_a_clone_server_the_user_has_xp_in(mem):
    fake, seen = mem
    saved = []

    async def ms(uid, all_bots=False):
        return [row(guild_id=11, clone_id=7, bot_username="Cloney")] if all_bots else []

    async def setp(gid, uid, optout):
        saved.append((gid, uid, optout))
    fake.member_servers, fake.member_level_ping_set = ms, setp
    st, p, _ = call("POST", {}, body={"action": "member_pref_set", "kind": "level_ping", "guild_id": "11", "value": True})
    assert st == 200 and saved == [(11, 6, True)]
    st, p, _ = call("POST", {}, body={"action": "member_pref_set", "kind": "level_ping", "guild_id": "999", "value": True})
    assert st == 404


def test_db_signature_defaults_to_main_only():
    assert inspect.signature(Database.member_servers).parameters["all_bots"].default is False
