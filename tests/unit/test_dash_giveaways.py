"""Giveaways page (A2, read-only): pure view, input validation, permissions, pagination, no entrant ids leaked."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
BIG = 1534574875274903562


def gw(i, status="active", entrants=(1, 2, 3), winners=()):
    return {"id": i, "channel_id": BIG, "message_id": BIG + 1, "host_id": BIG + 2, "prize": "Nitro", "winner_count": 2,
            "ends_at": NOW, "winner_ids": list(winners), "status": status, "entrant_ids": list(entrants)}


def test_view_stringifies_ids_counts_entrants_and_hides_them():
    v = S.giveaway_view(gw(7, winners=[BIG]))
    assert v["id"] == "7" and v["channel_id"] == str(BIG) and v["host_id"] == str(BIG + 2)
    assert v["entrants"] == 3 and v["winner_ids"] == [str(BIG)] and "entrant_ids" not in v
    assert S.giveaway_view({**gw(1), "entrant_count": 9})["entrants"] == 9


def test_view_cleans_prize_and_unknown_status():
    v = S.giveaway_view({**gw(1, status="weird"), "prize": "a\x00b\x1b[31m" + "x" * 500})
    assert v["status"] == "ended" and "\x00" not in v["prize"] and "\x1b" not in v["prize"] and len(v["prize"]) == S.GIVEAWAY_PRIZE_MAX


@pytest.fixture
def gws(env, monkeypatch):
    fake, members = env
    seen = {}

    async def rows(gid, clone, status, before, limit):
        seen["args"] = (gid, clone, status, before, limit)
        return [gw(i) for i in range(40, 40 - min(limit, 31), -1)]
    monkeypatch.setattr(fake, "dash_giveaways", rows, raising=False)
    return fake, members, seen


def q(**kw):
    return dict({"action": "giveaways", "guild_id": str(GUILD)}, **kw)


def test_requires_session_and_guild_permission(gws):
    _, members, _ = gws
    assert call("GET", q(), token=None)[0] == 401
    members["6"] = {"roles": []}
    assert call("GET", q())[0] == 403


def test_lists_and_pages(gws):
    _, _, seen = gws
    st, p, _ = call("GET", q())
    assert st == 200 and len(p["giveaways"]) == 30 and p["more"] is True
    assert seen["args"][0] == GUILD and seen["args"][2:] == (None, None, 31)
    assert all("entrant_ids" not in g for g in p["giveaways"])
    st, p, _ = call("GET", q(status="ended", before="12"))
    assert st == 200 and seen["args"][2:4] == ("ended", 12)


@pytest.mark.parametrize("kw", [{"status": "drop table"}, {"before": "abc"}, {"before": "0"}, {"before": str(2 ** 31)}])
def test_bad_input_is_rejected(gws, kw):
    assert call("GET", q(**kw))[0] == 400


def test_route_is_read_only():
    src = Path("api/dash.py").read_text()
    block = src[src.index('action == "giveaways"'):]
    block = block[:block.index('action == "welcome_preview"')]
    assert "POST" not in block and "set_giveaway" not in block and "finish_giveaway" not in block
