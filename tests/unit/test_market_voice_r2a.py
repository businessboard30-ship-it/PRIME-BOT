"""Market voice messages data layer (R2a): clip checks, send rules, limits, play claim, every path ends with the audio gone,
reports. Play game only. In-memory fake database plus static checks on the SQL."""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules import market_voice as mv

ROOT = Path(__file__).resolve().parents[2]
OGG = b"OggS" + b"\x00" * 200


def run(c):
    return asyncio.run(c)


class VoiceConn:
    """Seller 10 has an open listing 1 and voice on. Buyer 20. Voice rows live in self.rows."""

    def __init__(self):
        self.listing = dict(seller_id=10, status="open", ended=False)
        self.on = {10: True}
        self.blocks = set()
        self.rows, self.next = {}, 1
        self.reports = {}

    # helpers
    def _terminal_has_audio(self):
        return [r["id"] for r in self.rows.values() if r["status"] != "pending" and r["audio"] is not None]

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *a):
        if "FROM market_listings WHERE id" in sql:
            return self.listing
        if "FROM market_voice v WHERE v.id" in sql:
            r = self.rows.get(a[0])
            return dict(r, late=r["late"]) if r else None
        if "SELECT receiver_id, sender_id FROM market_voice WHERE id" in sql:
            return self.rows.get(a[0])
        if "SET status = 'deleted', audio = NULL, closed_at = NOW() WHERE id = $1 AND sender_id" in sql:
            r = self.rows.get(a[0])
            if r and r["sender_id"] == a[1] and r["status"] == "pending":
                r.update(status="deleted", audio=None)
                return {"card_channel": r["card_channel"], "card_message": r["card_message"]}

    async def fetchval(self, sql, *a):
        if "SELECT voice_on" in sql:
            return self.on.get(a[0])
        if "FROM cat_blocks" in sql:
            return 1 if (a[0], a[1]) in self.blocks or (a[1], a[0]) in self.blocks else None
        if "COUNT(*) FROM market_voice WHERE listing_id" in sql:
            return sum(1 for r in self.rows.values() if r["listing_id"] == a[0] and r["sender_id"] == a[1])
        if "COUNT(*) FROM market_voice WHERE sender_id" in sql:
            return sum(1 for r in self.rows.values() if r["sender_id"] == a[0])
        if "COUNT(*) FROM market_voice WHERE receiver_id" in sql:
            return sum(1 for r in self.rows.values() if r["receiver_id"] == a[0] and r["status"] == "pending")
        if sql.startswith("INSERT INTO market_voice "):
            i = self.next
            self.next += 1
            self.rows[i] = dict(id=i, listing_id=a[0], sender_id=a[1], receiver_id=a[2], duration_ms=a[3], size_bytes=a[4], audio=a[5],
                                status="pending", late=False, card_channel=None, card_message=None, play_channel=None, play_message=None, opened_recent=True)
            return i
        if "SET status = 'pending', audio = $2" in sql:
            r = self.rows.get(a[0])
            if r and r["status"] == "opened" and r["play_message"] is None and r["opened_recent"]:
                r.update(status="pending", audio=a[1])
                return r["id"]
        if sql.startswith("INSERT INTO market_reports"):
            if (a[0], a[1]) in self.reports:
                return None
            self.reports[(a[0], a[1])] = a[2]
            return len(self.reports)

    async def fetch(self, sql, *a):
        if "SET status = 'expired', audio = NULL" in sql:
            hit = [r for r in self.rows.values() if r["status"] == "pending" and r["late"]]
            for r in hit:
                r.update(status="expired", audio=None)
            return hit
        if "FROM market_listings l WHERE v.listing_id" in sql:
            hit = [r for r in self.rows.values() if r["status"] == "pending" and self.listing["status"] != "open"]
            for r in hit:
                r.update(status="deleted", audio=None)
            return hit
        if "WHERE receiver_id = $1 AND sender_id = $2 AND status = 'pending'" in sql:
            hit = [r for r in self.rows.values() if r["receiver_id"] == a[0] and r["sender_id"] == a[1] and r["status"] == "pending"]
            for r in hit:
                r.update(status="deleted", audio=None)
            return hit
        if "FROM market_voice WHERE status = 'opened'" in sql:
            return [r for r in self.rows.values() if r["status"] == "opened" and r.get("due")]
        if "FROM market_voice WHERE receiver_id = $1 AND status = 'pending'" in sql:
            return [dict(r, created_at=None, expires_at=None) for r in self.rows.values() if r["receiver_id"] == a[0] and r["status"] == "pending"]
        return []

    async def execute(self, sql, *a):
        if "SET status = 'expired', audio = NULL, closed_at = NOW() WHERE id = $1" in sql:
            self.rows[a[0]].update(status="expired", audio=None)
        elif "SET status = 'opened', audio = NULL" in sql:
            self.rows[a[0]].update(status="opened", audio=None)
        elif "SET status = 'deleted', audio = NULL, closed_at = NOW() WHERE id = $1 AND status = 'opened'" in sql:
            if self.rows[a[0]]["status"] == "opened":
                self.rows[a[0]].update(status="deleted", audio=None)
        elif "SET play_channel" in sql:
            self.rows[a[0]].update(play_message=a[2])
        elif "SET card_channel" in sql:
            self.rows[a[0]].update(card_channel=a[1], card_message=a[2])
        elif sql.startswith("INSERT INTO market_voice_settings"):
            self.on[a[0]] = a[1]


@pytest.fixture
def db(monkeypatch):
    conn = VoiceConn()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    async def get_pool():
        return Pool()
    monkeypatch.setattr(mv, "_pool", get_pool)
    return conn


def send(sender=20, listing=1, audio=OGG, ms=5000):
    return run(mv.send(sender, listing, audio, ms))


# ---------- clip checks ----------
def test_clip_checks():
    assert mv.check_audio(OGG, 5000) is None
    assert mv.check_audio(b"", 5000) == "bad" and mv.check_audio(None, 5000) == "bad"
    assert mv.check_audio(b"RIFF" + b"\0" * 50, 5000) == "bad"            # not Ogg
    assert mv.check_audio(OGG, 0) == "bad"
    assert mv.check_audio(OGG, mv.MAX_SECONDS * 1000 + 1) == "long"
    assert mv.check_audio(b"OggS" + b"\0" * mv.MAX_BYTES, 1000) == "big"


# ---------- sending ----------
def test_a_message_is_stored_for_the_seller(db):
    r = send()
    assert r.status == "ok" and r.receiver_id == 10 and db.rows[r.voice_id]["audio"] == OGG and db.rows[r.voice_id]["status"] == "pending"


def test_send_rules(db):
    assert send(sender=10).status == "own"
    db.on[10] = False
    assert send().status == "off"                                        # opt-in: off unless the seller turned it on
    db.on[10] = True
    db.blocks.add((10, 20))
    assert send().status == "blocked"
    db.blocks.clear(), db.blocks.add((20, 10))
    assert send().status == "blocked"                                    # either direction
    db.blocks.clear()
    db.listing["status"] = "sold"
    assert send().status == "no_listing"
    db.listing.update(status="open", ended=True)
    assert send().status == "no_listing"
    assert send(audio=b"nope").status == "bad" and db.rows == {}


def test_limits(db):
    for _ in range(mv.PER_LISTING):
        assert send().status == "ok"
    assert send().status == "limit_listing"
    db.rows.clear()
    for i in range(mv.PER_DAY):
        db.rows[i + 100] = dict(id=i + 100, listing_id=500 + i, sender_id=20, receiver_id=11, status="expired", audio=None)
    assert send().status == "limit_day"
    db.rows.clear()
    for i in range(mv.PENDING_PER_SELLER):
        db.rows[i + 100] = dict(id=i + 100, listing_id=1, sender_id=30 + i, receiver_id=10, status="pending", audio=OGG)
    assert send().status == "limit_seller"


# ---------- playing ----------
def test_play_hands_over_the_audio_once_and_clears_it(db):
    vid = send().voice_id
    assert run(mv.claim_play(vid, 99)).status == "not_yours" and db.rows[vid]["audio"] == OGG
    res = run(mv.claim_play(vid, 10))
    assert res.status == "ok" and res.audio == OGG and res.sender_id == 20 and res.listing_id == 1
    assert db.rows[vid]["status"] == "opened" and db.rows[vid]["audio"] is None
    assert run(mv.claim_play(vid, 10)).status == "gone"                  # a second tap gets nothing


def test_a_late_message_cannot_be_played_and_its_audio_goes(db):
    vid = send().voice_id
    db.rows[vid]["late"] = True
    assert run(mv.claim_play(vid, 10)).status == "late"
    assert db.rows[vid]["status"] == "expired" and db.rows[vid]["audio"] is None


def test_a_failed_send_can_put_the_message_back_but_only_before_it_was_posted(db):
    vid = send().voice_id
    res = run(mv.claim_play(vid, 10))
    assert run(mv.reopen(vid, res.audio)) is True and db.rows[vid]["status"] == "pending" and db.rows[vid]["audio"] == OGG
    run(mv.claim_play(vid, 10))
    run(mv.set_played(vid, 5, 6))
    assert run(mv.reopen(vid, OGG)) is False and db.rows[vid]["audio"] is None


# ---------- every path ends with the audio gone ----------
def test_every_way_a_message_can_end_leaves_no_audio(db):
    ids = []
    for s in (21, 22, 23, 24, 25):
        ids.append(send(sender=s).voice_id)
    played, expired, closed, blocked, withdrawn = ids
    run(mv.claim_play(played, 10))
    db.rows[played]["due"] = True
    for vid, _, _ in run(mv.due_to_delete()):
        run(mv.mark_deleted(vid))
    db.rows[expired]["late"] = True
    assert [x[0] for x in run(mv.expire_due())] == [expired]
    assert run(mv.purge_pair(10, 24)) and db.rows[blocked]["audio"] is None
    assert run(mv.withdraw(withdrawn, 25)) is not None
    db.listing["status"] = "cancelled"
    run(mv.purge_closed_listings())
    assert db._terminal_has_audio() == []
    assert all(r["status"] != "pending" for r in db.rows.values())        # nothing is left waiting with audio


def test_only_the_sender_can_withdraw_and_only_while_pending(db):
    vid = send().voice_id
    assert run(mv.withdraw(vid, 99)) is None and db.rows[vid]["audio"] == OGG
    assert run(mv.withdraw(vid, 20)) is not None and db.rows[vid]["audio"] is None
    assert run(mv.withdraw(vid, 20)) is None


# ---------- lists, reports, settings ----------
def test_pending_list_is_metadata_only(db):
    send()
    rows = run(mv.pending_for(10))
    assert len(rows) == 1 and not hasattr(rows[0], "audio")
    assert "audio" not in re.search(r"async def pending_for.*?return \[", (ROOT / "modules/market_voice.py").read_text(), re.S).group(0).split("SELECT")[1].split("FROM")[0]


def test_reports_are_for_the_receiver_once_each(db):
    vid = send().voice_id
    assert run(mv.report(vid, 99)) == "gone"
    assert run(mv.report(vid, 10)) == "ok" and run(mv.report(vid, 10)) == "again"
    assert db.reports[(vid, 10)] == 20
    run(mv.claim_play(vid, 10))
    assert run(mv.report(vid, 10)) == "again"                              # still reportable after play, no audio needed


def test_voice_is_off_until_the_seller_turns_it_on(db):
    db.on.clear()
    assert run(mv.voice_on(10)) is False
    run(mv.set_voice_on(10, True))
    assert db.on[10] is True


# ---------- static guarantees ----------
SRC = (ROOT / "modules/market_voice.py").read_text()


def test_every_update_that_ends_a_message_clears_the_audio():
    terminal = [s for s in re.findall(r"UPDATE market_voice v? ?SET status = '(?:expired|deleted)'[^\"]*", SRC)]
    assert terminal and all("audio = NULL" in s for s in terminal), terminal
    assert "SET status = 'opened', audio = NULL" in SRC


def test_the_database_itself_refuses_audio_on_finished_messages():
    sql = (ROOT / "database/migrations/067_market_voice.sql").read_text()
    assert "CHECK (status = 'pending' OR audio IS NULL)" in sql and "octet_length(audio) <= 400000" in sql
    assert "DROP" not in sql.upper() and "voice_on   BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "067_market_voice.sql" in (ROOT / "database.py").read_text()


def test_audio_is_never_logged_or_selected_into_lists():
    assert "logger.info" not in SRC and "print(" not in SRC
    assert SRC.count("SELECT id, sender_id, listing_id, duration_ms") == 1         # the list columns, no audio
    assert mv.MAX_BYTES <= 400_000 and mv.PRIVACY and "copy" in mv.PRIVACY
