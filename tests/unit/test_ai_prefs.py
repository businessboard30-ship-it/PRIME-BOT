"""Per-user AI preferences: storage, caching, and understanding requests in chat."""
import asyncio

import pytest

from modules import ai_prefs


def run(c):
    return asyncio.run(c)


class FakeConn:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("db down")
        if sql.startswith("INSERT"):
            uid, persona, mode = args
            self.store[uid] = (persona, mode)

    async def fetchrow(self, sql, uid):
        if self.fail:
            raise RuntimeError("db down")
        if uid in self.store:
            persona, mode = self.store[uid]
            return {"persona": persona, "voice_mode": mode}
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self_):
                return pool.conn

            async def __aexit__(self_, *a):
                return False
        return Ctx()


@pytest.fixture()
def db(monkeypatch):
    store = {}
    conn = FakeConn(store)

    async def pool():
        return FakePool(conn)
    monkeypatch.setattr(ai_prefs, "_pool", pool)
    monkeypatch.setattr(ai_prefs, "_table_ready", False)
    ai_prefs._cache.clear()
    yield store, conn
    ai_prefs._cache.clear()


def test_new_users_get_the_defaults(db):
    assert run(ai_prefs.get_prefs(1)) == ("genz", "auto")


def test_character_and_voice_choices_persist_independently(db):
    store, _ = db
    assert run(ai_prefs.set_character(1, "gentle"))
    assert run(ai_prefs.set_voice_mode(1, "off"))
    ai_prefs._cache.clear()                              # force a real read
    assert run(ai_prefs.get_prefs(1)) == ("gentle", "off")
    assert run(ai_prefs.set_character(1, "sensei"))
    assert store[1] == ("sensei", "off")                 # changing character kept the voice setting


def test_unknown_values_are_rejected(db):
    assert not run(ai_prefs.set_character(1, "pirate"))
    assert not run(ai_prefs.set_voice_mode(1, "loud"))
    assert run(ai_prefs.get_prefs(1)) == ("genz", "auto")


def test_corrupt_stored_values_fall_back_to_defaults(db):
    store, _ = db
    store[5] = ("deleted-character", "weird")
    assert run(ai_prefs.get_prefs(5)) == ("genz", "auto")


def test_database_trouble_never_breaks_chat(db):
    _, conn = db
    conn.fail = True
    assert run(ai_prefs.get_prefs(9)) == ("genz", "auto")
    assert not run(ai_prefs.set_character(9, "gentle"))  # reports failure instead of raising
    conn.fail = False
    assert run(ai_prefs.get_prefs(9)) == ("genz", "auto")   # the failure wasn't cached


@pytest.mark.parametrize("text,expected", [
    ("switch to gentle", "gentle"), ("change to gen z please", "genz"), ("talk like a sensei", "sensei"),
    ("sensei mode", "sensei"), ("change your character", "menu"), ("what characters can you be?", "menu"),
])
def test_requests_to_change_character_are_understood(text, expected):
    assert ai_prefs.detect_character_request(text) == expected


@pytest.mark.parametrize("text", [
    "show me anime characters", "pick a character from naruto", "be gentle with me",
    "who is the strongest character in bleach", "my gen z teacher is annoying",
])
def test_ordinary_chat_about_characters_does_not_trigger_a_switch(text):
    assert ai_prefs.detect_character_request(text) is None
