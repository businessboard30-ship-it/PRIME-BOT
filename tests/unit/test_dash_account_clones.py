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


def test_pages_ask_for_all_bots_including_clans(mem):
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
    assert "all_bots=True" in inspect.getsource(dash_member.member_clans)        # clans now cover custom bots too


def test_clans_read_each_server_from_the_bot_its_xp_belongs_to(mem, monkeypatch):
    fake, seen = mem
    log = []

    async def ms(uid, all_bots=False):
        log.append(("servers", all_bots))
        return [row(), row(guild_id=11, guild_name="Clone Hall", clone_id=7, bot_username="Cloney", total_xp=900)]

    async def card(gid, uid, clone_id=None):
        log.append(("card", gid, clone_id))
        return "clan_white_wolves.png" if clone_id == 7 else None

    async def chief(gid, uid, clone_id=None):
        return {"seat_rank": 1, "clan_slug": "Wolves", "user_id": uid} if clone_id == 7 else None

    async def seats(gid, clone_id=None):
        log.append(("seats", gid, clone_id))
        return []

    async def count(gid, clan_card, clone_id=None):
        log.append(("count", gid, clone_id))
        return 5

    async def profiles(ids):
        return {}
    for name, fn in [("member_servers", ms), ("get_clan_card_if_assigned", card), ("get_chief_seat_for_user", chief),
                     ("get_clan_seats", seats), ("get_clan_member_count", count), ("board_profiles", profiles)]:
        monkeypatch.setattr(fake, name, fn, raising=False)
    st, p, _ = call("GET", {"action": "member_clans"})
    assert st == 200 and log[0] == ("servers", True)
    main, clone = p["servers"]
    assert main["bot"] is None and main["clan"] is None and main["chief"] is None          # main bot rows: clone_id None
    assert clone["bot"] == "Cloney" and clone["clan"]["members"] == 5 and clone["chief"]["seat"] == 1
    assert ("card", 10, None) in log and ("card", 11, 7) in log and ("seats", 11, 7) in log and ("count", 11, 7) in log


def _lang_fakes(fake, monkeypatch, rows):
    saved, langs = [], {0: "en", 7: "es"}

    async def ms(uid, all_bots=False):
        return rows if all_bots else [r for r in rows if r["clone_id"] is None]

    async def gl(uid, clone_id=0):
        return langs.get(clone_id, "en")

    async def sl(uid, lang, clone_id=0):
        saved.append((uid, lang, clone_id))
    for name, fn in [("member_servers", ms), ("get_user_language", gl), ("set_user_language", sl),
                     ("get_user_currency", None), ]:
        if fn is not None:
            monkeypatch.setattr(fake, name, fn, raising=False)

    async def cur(uid):
        return ""

    async def prefs(uid):
        return ("genz", "auto")
    monkeypatch.setattr(fake, "get_user_currency", cur, raising=False)
    from modules import ai_prefs
    monkeypatch.setattr(ai_prefs, "get_prefs", prefs)
    return saved


def test_prefs_lists_a_language_per_custom_bot_the_member_uses(mem, monkeypatch):
    fake, _ = mem
    _lang_fakes(fake, monkeypatch, [row(), row(guild_id=11, clone_id=7, bot_username="Cloney"),
                                    row(guild_id=12, clone_id=7, bot_username="Cloney")])
    st, p, _ = call("GET", {"action": "member_prefs"})
    assert st == 200 and p["language"] == "en"
    assert p["bot_languages"] == [{"clone": "7", "bot": "Cloney", "language": "es"}]     # once per bot, not per server


def test_language_can_be_set_per_custom_bot_and_main_is_unchanged(mem, monkeypatch):
    fake, _ = mem
    saved = _lang_fakes(fake, monkeypatch, [row(guild_id=11, clone_id=7, bot_username="Cloney")])
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "fr", "clone": "7"})[0] == 200
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "de"})[0] == 200
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "it", "clone": "0"})[0] == 200
    assert saved == [(6, "fr", 7), (6, "de", 0), (6, "it", 0)]


def test_language_for_a_bot_the_member_has_no_xp_on_is_refused(mem, monkeypatch):
    fake, _ = mem
    saved = _lang_fakes(fake, monkeypatch, [row(guild_id=11, clone_id=7, bot_username="Cloney")])
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "fr", "clone": "99"})[0] == 404
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "fr", "clone": "abc"})[0] == 400
    assert call("POST", body={"action": "member_pref_set", "kind": "language", "value": "xx", "clone": "7"})[0] == 400
    assert saved == []


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
