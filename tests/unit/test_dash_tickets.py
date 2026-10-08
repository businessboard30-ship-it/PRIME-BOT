from datetime import datetime, timezone

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ROW = {"id": 7, "guild_id": GUILD, "channel_id": 4242, "opener_id": 9, "claimed_by": None,
       "status": "closed", "created_at": NOW, "closed_at": NOW}


def test_row_and_message_views_are_bounded_plain_data():
    v = S.ticket_row_view(ROW)
    assert v["id"] == "7" and v["opener_id"] == "9" and v["status"] == "closed" and v["claimed_by"] is None
    m = S.ticket_message_view({"id": 1, "timestamp": "t", "content": "x" * 5000,
                               "author": {"username": "a", "bot": True},
                               "attachments": [{"filename": "f.png"}], "embeds": [{}, {}]})
    assert len(m["text"]) == S.TICKET_MSG_MAX and m["bot"] and m["files"] == ["f.png"] and m["embeds"] == 2


class TicketDB:
    def __init__(self, base):
        self.base, self.last = base, None
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def list_tickets(self, gid, cid, status, before, limit):
        self.last = (gid, status, before, limit)
        return [dict(ROW, id=i) for i in range(40, 40 - limit, -1)]
    async def get_ticket_by_id(self, gid, cid, tid):
        return ROW if (gid == GUILD and tid == 7) else None


@pytest.fixture
def tk(env, monkeypatch):
    fake, members = env
    db = TicketDB(fake)
    monkeypatch.setattr(dash, "db", db)
    orig = dash._bot_get
    state = {"pages": {}}

    async def bot_get(path):
        if path.startswith("/channels/4242/messages"):
            if state.get("err"):
                raise dash.DiscordError(state["err"])
            return state["pages"].get("p", [])
        return await orig(path)
    monkeypatch.setattr(dash, "_bot_get", bot_get)
    return db, state


def test_list_paginates_and_filters(tk):
    db, _ = tk
    st, p, _ = call("GET", {"action": "tickets", "guild_id": str(GUILD), "status": "open", "before": "30"})
    assert st == 200 and len(p["tickets"]) == 30 and p["more"] is True and db.last == (GUILD, "open", 30, 31)
    assert call("GET", {"action": "tickets", "guild_id": str(GUILD), "status": "weird"})[0] == 400
    assert call("GET", {"action": "tickets", "guild_id": str(GUILD), "before": "x"})[0] == 400
    assert call("GET", {"action": "tickets", "guild_id": "999"})[0] == 403


def test_messages_oldest_first_and_channel_from_our_row(tk):
    _, state = tk
    state["pages"]["p"] = [{"id": "2", "content": "second", "author": {"username": "b"}, "timestamp": "t2"},
                           {"id": "1", "content": "first", "author": {"username": "a"}, "timestamp": "t1"}]
    st, p, _ = call("GET", {"action": "ticket_messages", "guild_id": str(GUILD), "id": "7"})
    assert st == 200 and [m["text"] for m in p["messages"]] == ["first", "second"] and not p["gone"]
    assert call("GET", {"action": "ticket_messages", "guild_id": str(GUILD), "id": "8"})[0] == 404   # not this server's / unknown
    assert call("GET", {"action": "ticket_messages", "guild_id": str(GUILD), "id": "zz"})[0] == 400


def test_deleted_channel_is_reported_not_an_error(tk):
    _, state = tk
    state["err"] = 404
    st, p, _ = call("GET", {"action": "ticket_messages", "guild_id": str(GUILD), "id": "7"})
    assert st == 200 and p["gone"] is True and p["messages"] is None
