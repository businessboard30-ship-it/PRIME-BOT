"""Moderation page (A1): pure views, input validation, permissions, pagination; step 2 adds the audited single-warn remove."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
BIG = 1534574875274903562


def case(i, kind="warn", target=BIG, by=222222222222, reason="spam"):
    return {"id": i, "action_type": kind, "target_user_id": target, "performed_by": by, "reason": reason, "created_at": NOW}


# ---------- pure ----------
def test_case_view_stringifies_ids_clips_and_strips_control_chars():
    v = S.mod_case_view(case(7, reason="a\x00b\x1b[31m" + "x" * 500))
    assert v["id"] == "7" and v["target_id"] == str(BIG) and v["by"] == "222222222222"
    assert "\x00" not in v["reason"] and "\x1b" not in v["reason"] and len(v["reason"]) == S.MOD_REASON_MAX
    assert S.mod_case_view(case(1, target=None))["target_id"] is None
    assert S.mod_case_view(case(1, reason=None))["reason"] == ""


def test_warn_view():
    v = S.mod_warn_view({"id": 3, "reason": None, "warned_by": 5, "created_at": NOW})
    assert v == {"id": "3", "by": "5", "reason": "", "at": NOW.isoformat()}


@pytest.mark.parametrize("raw,ok", [(None, True), ("", True), (str(BIG), True), ("123", False), ("abc", False),
                                    ("1" * 21, False), ("9" * 19, False), ("-5555555555", False), ("12345678901 ", True)])
def test_parse_mod_user(raw, ok):
    if ok:
        S.parse_mod_user(raw)
    else:
        with pytest.raises(ValueError):
            S.parse_mod_user(raw)


# ---------- route ----------
@pytest.fixture
def mod(env, monkeypatch):
    fake, members = env
    seen = {}

    async def cases(gid, user, kind, before, limit):
        seen["cases"] = (gid, user, kind, before, limit)
        return [case(i) for i in range(40, 40 - min(limit, 31), -1)]

    async def warns(gid, user, limit=50):
        seen["warns"] = (gid, user)
        return [{"id": 1, "reason": "r", "warned_by": 9, "created_at": NOW}], 4

    async def kinds(gid):
        return ["ban", "warn", "Bad Kind!"]
    monkeypatch.setattr(fake, "dash_mod_cases", cases, raising=False)
    monkeypatch.setattr(fake, "dash_mod_warns", warns, raising=False)
    monkeypatch.setattr(fake, "dash_mod_kinds", kinds, raising=False)
    return fake, members, seen


def q(**kw):
    return dict({"action": "moderation", "guild_id": str(GUILD)}, **kw)


def test_requires_session_and_guild_permission(mod):
    _, members, _ = mod
    assert call("GET", q(), token=None)[0] == 401
    members["6"] = {"roles": []}                      # signed in but no Manage Server here
    assert call("GET", q())[0] == 403


def test_first_page_lists_cases_kinds_and_pages(mod):
    _, _, seen = mod
    st, p, _ = call("GET", q())
    assert st == 200 and len(p["cases"]) == 30 and p["more"] is True
    assert p["kinds"] == ["ban", "warn"]              # invalid kind names are dropped
    assert "warns" not in p and seen["cases"] == (GUILD, None, None, None, 31)


def test_user_filter_adds_warns_and_count_on_first_page_only(mod):
    _, _, seen = mod
    st, p, _ = call("GET", q(user_id=str(BIG), kind="warn"))
    assert st == 200 and p["warn_count"] == 4 and p["warns"][0]["by"] == "9"
    assert seen["cases"][1:3] == (BIG, "warn") and seen["warns"] == (GUILD, BIG)
    st, p, _ = call("GET", q(user_id=str(BIG), before="25"))
    assert "warns" not in p and "kinds" not in p and seen["cases"][3] == 25


@pytest.mark.parametrize("extra", [{"user_id": "abc"}, {"user_id": "12"}, {"kind": "x; DROP TABLE"}, {"kind": "A" * 5},
                                   {"before": "zzz"}, {"before": "0"}, {"before": str(2 ** 40)}])
def test_bad_input_is_a_400_and_never_reaches_the_database(mod, extra):
    _, _, seen = mod
    assert call("GET", q(**extra))[0] == 400 and "cases" not in seen


def test_guild_id_comes_from_the_authorised_guild(mod):
    _, _, seen = mod
    assert call("GET", q(guild_id="999999999999"))[0] in (403, 404)
    assert "cases" not in seen


def test_route_is_read_only_and_page_has_no_innerhtml():
    src = Path(dash.__file__).read_text()
    i = src.index('action == "moderation"'); block = src[i:i + 1400]
    assert "method ==" in src[i - 40:i] and 'method == "GET"' in src[i - 40:i]
    assert not any(w in block for w in ("INSERT", "DELETE", "UPDATE", "clear_warns", "add_warn"))
    js = (Path(dash.__file__).resolve().parents[1] / "dashboard" / "assets" / "dash.js").read_text()
    page = js[js.index("function renderModeration"):js.index("function renderTickets")]
    assert "innerHTML" not in page and '"moderation", "gavel"' not in page
    assert 'gpath(gid, "moderation")' in js and "moderation: renderModeration" in js


# ---------- A1 step 2: remove ONE warn (audited) ----------
MOD_BIT = 1 << 40


@pytest.fixture
def wr(env, monkeypatch):
    """User 6 is in guild 111 with role 500 (Manage Server). Roles: 503 adds Timeout Members, 504 is Administrator."""
    fake, members = env
    from tests.unit import test_dash_api as T
    monkeypatch.setitem(T.INFO, "roles", T.INFO["roles"] + [
        {"id": "503", "permissions": str(MOD_BIT), "position": 3, "name": "Timeout"},
        {"id": "504", "permissions": str(0x8), "position": 4, "name": "Admin"}])
    st = {"deleted": [], "gone": {"reason": "spam", "remaining": 2}, "audit": [], "modlog": []}

    async def remove(gid, user, warn):
        st["deleted"].append((gid, user, warn))
        return st["gone"]

    async def audit(gid, user_id, user_name, module, changes, retention=180):
        st["audit"].append((gid, user_id, user_name, module, changes))
    monkeypatch.setattr(fake, "dash_warn_remove", remove, raising=False)
    monkeypatch.setattr(fake, "dash_audit_add", audit, raising=False)
    mx = __import__("modules.moderation_extra", fromlist=["x"])

    async def log_action(chat_id, action_type, performed_by, target_user_id=None, reason=""):
        st["modlog"].append((chat_id, action_type, performed_by, target_user_id, reason))
        return True
    monkeypatch.setattr(mx, "log_action", log_action)
    members["6"] = {"roles": ["500", "503"]}
    dash._cache.clear(); dash._owner_hits.clear()
    return st, members


def rm(**over):
    body = {"action": "warn_remove", "guild_id": str(GUILD), "user_id": str(BIG), "warn_id": "7", **over}
    dash._owner_hits.clear()
    return call("POST", None, body=body)


def test_remove_needs_a_session_and_server_access(wr):
    dash._owner_hits.clear()
    assert call("POST", None, token=None, body={"action": "warn_remove", "guild_id": str(GUILD), "user_id": str(BIG), "warn_id": "1"})[0] == 401
    assert rm(guild_id="999")[0] == 403                                   # not one of their servers
    assert wr[0]["deleted"] == []


def test_manage_server_alone_is_not_enough_you_need_timeout_members(wr):
    st, members = wr
    members["6"] = {"roles": ["500"]}                                     # Manage Server, no Timeout Members
    dash._cache.clear()
    status, p, _ = rm()
    assert status == 403 and "Timeout Members" in p["message"] and st["deleted"] == [] and st["audit"] == []


def test_timeout_members_administrator_and_owner_can_remove(wr):
    st, members = wr
    assert rm()[0] == 200
    members["6"] = {"roles": ["500", "504"]}; dash._cache.clear()         # Administrator
    assert rm(warn_id="8")[0] == 200
    from tests.unit import test_dash_api as T
    members["6"] = {"roles": ["500"]}; dash._cache.clear()
    old = T.INFO["owner_id"]
    T.INFO["owner_id"] = "6"
    try:
        assert rm(warn_id="9")[0] == 200                                  # server owner
    finally:
        T.INFO["owner_id"] = old
    assert [d[2] for d in st["deleted"]] == [7, 8, 9]


def test_remove_deletes_one_warn_scoped_to_server_and_member_and_returns_the_new_count(wr):
    st, _ = wr
    status, p, _ = rm(user_id=f" {BIG} ", warn_id=" 7 ")
    assert status == 200 and p["message"] == "Warn removed." and p["warn_count"] == 2
    assert st["deleted"] == [(GUILD, BIG, 7)]                             # guild comes from the authorised server, ids parsed


def test_remove_is_audited_and_leaves_an_unwarn_case_like_discord(wr):
    st, _ = wr
    rm()
    ((gid, uid, name, module, changes),) = st["audit"]
    assert gid == GUILD and uid == "6" and module == "moderation"
    (label, change), = changes.items()
    assert "Warn #7" in label and str(BIG) in label and change == {"from": "spam", "to": None}
    assert st["modlog"] == [(GUILD, "unwarn", 6, BIG, "Removed warn #7 via dashboard")]


def test_audit_text_is_clipped_and_control_characters_removed(wr):
    st, _ = wr
    st["gone"] = {"reason": "x\x00y\x1b" + "z" * 500, "remaining": 0}
    rm()
    frm = list(st["audit"][0][4].values())[0]["from"]
    assert "\x00" not in frm and "\x1b" not in frm and len(frm) <= 120


@pytest.mark.parametrize("over", [{"warn_id": "abc"}, {"warn_id": ""}, {"warn_id": "0"}, {"warn_id": "-1"}, {"warn_id": str(2 ** 31)},
                                  {"warn_id": "1 2"}, {"warn_id": None}, {"user_id": "123"}, {"user_id": "abc"}, {"user_id": ""}, {"user_id": None}])
def test_bad_ids_are_rejected_before_anything_happens(wr, over):
    st, _ = wr
    assert rm(**over)[0] == 400 and st["deleted"] == [] and st["audit"] == [] and st["modlog"] == []


def test_a_warn_that_is_not_theirs_or_already_gone_is_a_404_with_no_audit(wr):
    st, _ = wr
    st["gone"] = None
    status, p, _ = rm()
    assert status == 404 and "already gone" in p["message"] and st["audit"] == [] and st["modlog"] == []


def test_a_failing_audit_or_modlog_does_not_hide_that_the_warn_was_removed(wr, monkeypatch):
    st, _ = wr
    fake = dash.db

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(fake, "dash_audit_add", boom, raising=False)
    assert rm()[0] == 200 and st["deleted"]


def test_remove_is_rate_limited(wr):
    dash._owner_hits.clear()
    codes = [call("POST", None, body={"action": "warn_remove", "guild_id": str(GUILD), "user_id": str(BIG), "warn_id": str(i + 1)})[0] for i in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_there_is_no_web_clear_all_and_discord_unwarn_is_unchanged():
    src = Path(dash.__file__).read_text()
    assert "warn_clear" not in src and "clear_warns" not in src          # "clear all" stays on Discord
    cog = (Path(dash.__file__).parent.parent / "discord_bot" / "cogs" / "moderation.py").read_text()
    assert "await mod.clear_warns(member.id, confirm_interaction.guild_id)" in cog


def test_remove_button_is_text_only_and_confirms_first():
    js = (Path(dash.__file__).parent.parent / "dashboard" / "assets" / "dash.js").read_text()
    i = js.index("function renderModeration(")
    body = js[i:js.index("\n  /* ----------", i)]
    assert "innerHTML" not in body and 'api("warn_remove"' in body and "confirm(" in body
