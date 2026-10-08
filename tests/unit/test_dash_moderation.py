"""Moderation page (A1, read-only): pure views, input validation, permissions, pagination, no writes."""
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
