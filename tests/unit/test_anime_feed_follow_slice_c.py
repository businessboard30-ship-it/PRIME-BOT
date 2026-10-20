"""Anime feed slice C: follows (free limit, race-safe toggle), ping role and follower mentions, button wiring. No network."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from discord_bot.cogs import _views_anime_feed as vf
from discord_bot.cogs import _views_join_dm as jd
from modules import anime_feed as af
from modules import anime_feed_worker as aw
from tests.unit.test_anime_feed_worker_slice_b import Chan, P, cfg

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def test_ping_line_caps_mentions_dedupes_and_counts_the_rest():
    line, shown = af.ping_line(None, [])
    assert line == "" and shown == []
    line, shown = af.ping_line(55, [1, 2, 2, 3])
    assert line == "<@&55> <@1> <@2> <@3>" and shown == [1, 2, 3]
    line, shown = af.ping_line(None, list(range(1, 26)))
    assert len(shown) == af.MAX_FOLLOWER_PINGS and line.endswith("and 15 more following")


class DB:
    def __init__(self, rows=()):
        self.rows = set(rows)
        self.locks = []

    def acquire(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def transaction(self): return self

    async def execute(self, sql, *a):
        if "pg_advisory_xact_lock" in sql:
            self.locks.append(a[0])
        elif sql.startswith("INSERT INTO anime_follows"):
            self.rows.add((a[0], a[1]))

    async def fetchval(self, sql, *a):
        if sql.startswith("DELETE FROM anime_follows"):
            if (a[0], a[1]) in self.rows:
                self.rows.discard((a[0], a[1]))
                return 1
            return None
        if "COUNT(*)" in sql:
            return len([r for r in self.rows if r[0] == a[0]])


def test_follow_toggle_enforces_the_free_limit_under_a_lock_and_unfollow_frees_a_slot(monkeypatch):
    db = DB()

    async def pool():
        return db
    monkeypatch.setattr(af, "_pool", pool)
    for i in range(1, af.FREE_FOLLOW_LIMIT + 1):
        assert run(af.toggle_follow(7, i, f"A{i}")) == ("followed", i)
    assert run(af.toggle_follow(7, 99, "Over")) == ("limit", af.FREE_FOLLOW_LIMIT) and (7, 99) not in db.rows
    assert run(af.toggle_follow(7, 3, "A3")) == ("unfollowed", af.FREE_FOLLOW_LIMIT - 1)
    assert run(af.toggle_follow(7, 99, "Over")) == ("followed", af.FREE_FOLLOW_LIMIT)
    assert run(af.toggle_follow(8, 1, "Other person")) == ("followed", 1)                   # limits are per person
    assert set(db.locks) == {7, 8}


class Guild:
    def __init__(self, members, chan):
        self.members, self.chan = set(members), chan

    def get_channel(self, cid): return self.chan
    def get_member(self, uid): return object() if uid in self.members else None


def test_post_pings_the_role_and_only_followers_who_are_in_that_server(monkeypatch):
    now = int(time.time())
    chan = Chan()
    guild = Guild({1, 3}, chan)
    calls = SimpleNamespace(claims=0)

    async def claim(*a):
        return True

    async def followers(anime_id, limit=500):
        return [1, 2, 3]
    monkeypatch.setattr(af, "claim", claim)
    monkeypatch.setattr(af, "followers_of", followers)
    w = aw.Worker(SimpleNamespace(get_guild=lambda g: guild), None, post_gap=0)
    c = af.FeedConfig(1, None, True, 100, datetime.fromtimestamp(now - 100, timezone.utc), ping_role_id=55)
    assert run(w._post(c, P(1, at=now - 5))) is True
    sent = chan.sent[0]
    text = " ".join(getattr(x, "content", "") or "" for x in sent["view"].walk_children())
    assert "<@&55>" in text and "<@1>" in text and "<@3>" in text and "<@2>" not in text
    am = sent["allowed_mentions"]
    assert [u.id for u in am.users] == [1, 3] and [r.id for r in am.roles] == [55] and am.everyone is False


def test_post_without_role_or_followers_pings_nobody(monkeypatch):
    now = int(time.time())
    chan = Chan()

    async def claim(*a):
        return True

    async def followers(anime_id, limit=500):
        return []
    monkeypatch.setattr(af, "claim", claim)
    monkeypatch.setattr(af, "followers_of", followers)
    w = aw.Worker(SimpleNamespace(get_guild=lambda g: Guild(set(), chan)), None, post_gap=0)
    run(w._post(cfg(1, now - 100), P(1, at=now - 5)))
    am = chan.sent[0]["allowed_mentions"]
    assert am.users == [] and am.roles is False or am.roles == []


def test_every_card_has_a_follow_button_with_a_valid_persistent_id_and_still_fits():
    p = P(42, at=2_000_000_000)
    kids = list(vf.post_view(p, ping="<@&5> <@1>").walk_children())
    ids = [getattr(c, "custom_id", None) for c in kids]
    assert "anime_follow:42" in ids and sum(1 for _ in kids) <= 40
    assert vf._FollowButton in jd.DYNAMIC_ITEMS and all(len(i) <= 100 for i in ids if i)


def test_follow_button_replies_privately_with_the_count_and_the_limit_message(monkeypatch):
    sent = []

    async def toggle(user_id, anime_id, title, limit=5):
        return "limit", 5

    async def free(user_id, guild_id=None, clone_id=None):
        return 5
    from modules import anime_plus
    monkeypatch.setattr(anime_plus, "follow_limit", free)
    monkeypatch.setattr(af, "toggle_follow", toggle)
    inter = SimpleNamespace(user=SimpleNamespace(id=7), message=None, guild=None, client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(send_message=AsyncMock(side_effect=lambda *a, **k: sent.append((a, k)))))
    run(vf._FollowButton(42).callback(inter))
    assert sent and sent[0][1]["ephemeral"] is True and "your limit" in sent[0][0][0] and "Upgrade" in sent[0][0][0]


def test_title_is_read_from_the_card_heading_and_survives_odd_messages():
    td = SimpleNamespace(content="## Frieren\nEpisode 3")
    msg = SimpleNamespace(components=[SimpleNamespace(children=[SimpleNamespace(accessory=None, components=[td])])])
    assert vf._title_from_message(SimpleNamespace(message=msg)) == "Frieren"
    assert vf._title_from_message(SimpleNamespace(message=None)) is None


def test_wizard_with_the_role_picker_fits_and_clears_or_rejects_roles(monkeypatch):
    saved = []

    async def save(c, by=None):
        saved.append(c.ping_role_id)
    monkeypatch.setattr(af, "save_config", save)
    role = SimpleNamespace(id=9, mention="<@&9>", mentionable=True, managed=False, is_default=lambda: False)
    managed = SimpleNamespace(id=8, mention="<@&8>", mentionable=True, managed=True, is_default=lambda: False)
    guild = SimpleNamespace(id=1, me=SimpleNamespace(guild_permissions=SimpleNamespace(mention_everyone=False)),
                            get_role=lambda i: {9: role, 8: managed}.get(i), get_channel=lambda i: None)
    v = vf.AnimeSetupView(guild, None, 7, af.FeedConfig(1, None, True, 55, None, 9))
    assert sum(1 for _ in v.walk_children()) <= 40
    v._push = AsyncMock()
    run(v._on_role(SimpleNamespace(user=SimpleNamespace(id=7), data={"values": ["9"]})))
    assert saved == [9] and v.cfg.ping_role_id == 9
    run(v._on_role(SimpleNamespace(user=SimpleNamespace(id=7), data={"values": ["8"]})))
    assert saved == [9] and "normal role" in v.note
    run(v._on_role(SimpleNamespace(user=SimpleNamespace(id=7), data={"values": []})))
    assert saved == [9, None]


def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/072_anime_follow_ping.sql").read_text()
    assert "ping_role_id" in sql and "CREATE TABLE IF NOT EXISTS anime_follows" in sql and "DROP" not in sql.upper()
    assert "072_anime_follow_ping.sql" in (ROOT / "database.py").read_text()
