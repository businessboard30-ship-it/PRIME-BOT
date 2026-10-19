# path: modules/market_voice.py

"""
Market voice messages (R2a). Play game only. A buyer sends ONE short voice message to the seller of an open listing; the bot
relays it. This module is the data layer and the rules. Discord handling (receive, delete the original, the seller's card,
the Play button, timers) is R2b.

What the bot can and cannot do (be honest on screens): it cannot know when Discord shows a voice message to someone, so the
seller taps **Play**. That tap sends the audio and starts a short timer, after which the bot deletes the message. Discord
may have already cached or recorded it elsewhere, so the wording is "removed from the bot and from Discord", never
"impossible to save".

Storage: audio bytes are kept ONLY while a message is `pending`. Every other status has audio NULL (a database CHECK and
every UPDATE here guarantee it). Lists never read the audio column. Nothing here logs audio.
Statuses: pending -> opened (Play) -> deleted (timer), or pending -> expired / deleted (withdrawn, listing closed, blocked).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_SECONDS = 30
MAX_BYTES = 400_000                 # a 30 s Discord voice message is about 100 KB
EXPIRY_HOURS = 24                   # a message nobody plays is removed after this
PLAY_KEEP_SECONDS = 120             # the played message is deleted after this
PER_LISTING = 3                     # voice messages one buyer may ever send about one listing
PER_DAY = 10                        # per buyer, rolling 24 hours, across every listing
PENDING_PER_SELLER = 20             # unplayed messages one seller can hold at once
KEEP_DAYS = 30                      # metadata of finished messages is kept this long (reports need it), then pruned

PRIVACY = ("Voice messages go through the bot to the seller. They are removed from the bot when played, after "
           f"{EXPIRY_HOURS} hours, or when the listing ends. The bot deletes the played message a couple of minutes after the "
           "seller taps Play. Discord or other people can still keep their own copy, so only send what you are happy to share.")


async def _pool():
    from database import get_pool
    return await get_pool()


def check_audio(audio: Optional[bytes], duration_ms: int) -> Optional[str]:
    """None when the clip is acceptable, else bad | big | long. Only Ogg audio (what Discord voice messages are) passes."""
    if not audio or int(duration_ms) <= 0 or bytes(audio[:4]) != b"OggS":
        return "bad"
    if len(audio) > MAX_BYTES:
        return "big"
    if int(duration_ms) > MAX_SECONDS * 1000:
        return "long"
    return None


@dataclass(frozen=True)
class SendResult:
    status: str             # ok | bad | big | long | no_listing | own | off | blocked | limit_listing | limit_day | limit_seller
    voice_id: int = 0
    receiver_id: int = 0


@dataclass(frozen=True)
class PlayResult:
    status: str             # ok | gone | not_yours | late
    audio: Optional[bytes] = None
    sender_id: int = 0
    listing_id: int = 0


@dataclass(frozen=True)
class Pending:              # metadata only, never audio
    id: int
    sender_id: int
    listing_id: int
    duration_ms: int
    created_at: object
    expires_at: object


# ── settings ────────────────────────────────────────────────────────────

async def voice_on(owner_id: int) -> bool:
    try:
        p = await _pool()
        async with p.acquire() as conn:
            return bool(await conn.fetchval("SELECT voice_on FROM market_voice_settings WHERE owner_id = $1", int(owner_id)))
    except Exception:
        return False


async def set_voice_on(owner_id: int, on: bool) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("INSERT INTO market_voice_settings (owner_id, voice_on) VALUES ($1, $2) "
                           "ON CONFLICT (owner_id) DO UPDATE SET voice_on = EXCLUDED.voice_on, updated_at = NOW()",
                           int(owner_id), bool(on))


# ── sending ─────────────────────────────────────────────────────────────

async def send(sender_id: int, listing_id: int, audio: bytes, duration_ms: int) -> SendResult:
    """Store one voice message for the seller of an open listing. One transaction; the rules are checked inside it."""
    bad = check_audio(audio, duration_ms)
    if bad:
        return SendResult(bad)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            l = await conn.fetchrow("SELECT seller_id, status, (expires_at <= NOW()) AS ended FROM market_listings WHERE id = $1 FOR SHARE",
                                    int(listing_id))
            if l is None or l["status"] != "open" or l["ended"]:
                return SendResult("no_listing")
            seller = int(l["seller_id"])
            if seller == int(sender_id):
                return SendResult("own")
            if not await conn.fetchval("SELECT voice_on FROM market_voice_settings WHERE owner_id = $1", seller):
                return SendResult("off")
            if await conn.fetchval("SELECT 1 FROM cat_blocks WHERE (owner_id = $1 AND blocked_id = $2) OR (owner_id = $2 AND blocked_id = $1)",
                                   seller, int(sender_id)):
                return SendResult("blocked")
            if int(await conn.fetchval("SELECT COUNT(*) FROM market_voice WHERE listing_id = $1 AND sender_id = $2", int(listing_id), int(sender_id)) or 0) >= PER_LISTING:
                return SendResult("limit_listing")
            if int(await conn.fetchval("SELECT COUNT(*) FROM market_voice WHERE sender_id = $1 AND created_at > NOW() - INTERVAL '1 day'", int(sender_id)) or 0) >= PER_DAY:
                return SendResult("limit_day")
            if int(await conn.fetchval("SELECT COUNT(*) FROM market_voice WHERE receiver_id = $1 AND status = 'pending'", seller) or 0) >= PENDING_PER_SELLER:
                return SendResult("limit_seller")
            vid = await conn.fetchval(
                "INSERT INTO market_voice (listing_id, sender_id, receiver_id, duration_ms, size_bytes, audio, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, NOW() + $7 * INTERVAL '1 hour') RETURNING id",
                int(listing_id), int(sender_id), seller, int(duration_ms), len(audio), bytes(audio), EXPIRY_HOURS)
    return SendResult("ok", int(vid), seller)


async def set_card(voice_id: int, channel_id: int, message_id: int) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE market_voice SET card_channel = $2, card_message = $3 WHERE id = $1 AND status = 'pending'",
                           int(voice_id), int(channel_id), int(message_id))


# ── playing ─────────────────────────────────────────────────────────────

async def claim_play(voice_id: int, user_id: int) -> PlayResult:
    """The seller taps Play. In ONE transaction: take the audio out of the database and mark the message opened. The caller
    sends the bytes; if sending fails it calls `reopen`."""
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            v = await conn.fetchrow("SELECT id, sender_id, receiver_id, listing_id, status, audio, (expires_at <= NOW()) AS late "
                                    "FROM market_voice v WHERE v.id = $1 FOR UPDATE", int(voice_id))
            if v is None or v["status"] != "pending":
                return PlayResult("gone")
            if int(v["receiver_id"]) != int(user_id):
                return PlayResult("not_yours")
            if v["late"]:
                await conn.execute("UPDATE market_voice SET status = 'expired', audio = NULL, closed_at = NOW() WHERE id = $1", int(voice_id))
                return PlayResult("late")
            audio = bytes(v["audio"])
            await conn.execute("UPDATE market_voice SET status = 'opened', audio = NULL, opened_at = NOW(), "
                               "delete_after = NOW() + $2 * INTERVAL '1 second' WHERE id = $1", int(voice_id), PLAY_KEEP_SECONDS)
    return PlayResult("ok", audio, int(v["sender_id"]), int(v["listing_id"]))


async def reopen(voice_id: int, audio: bytes) -> bool:
    """Sending the audio to Discord failed right after Play: put the message back so the seller can try again."""
    p = await _pool()
    async with p.acquire() as conn:
        return await conn.fetchval(
            "UPDATE market_voice SET status = 'pending', audio = $2, opened_at = NULL, delete_after = NULL "
            "WHERE id = $1 AND status = 'opened' AND play_message IS NULL AND opened_at > NOW() - INTERVAL '2 minutes' RETURNING id",
            int(voice_id), bytes(audio)) is not None


async def set_played(voice_id: int, channel_id: int, message_id: int) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE market_voice SET play_channel = $2, play_message = $3 WHERE id = $1 AND status = 'opened'",
                           int(voice_id), int(channel_id), int(message_id))


# ── cleanup (every path ends with the bytes gone) ───────────────────────

async def due_to_delete(limit: int = 50) -> List[Tuple[int, Optional[int], Optional[int]]]:
    """Played messages whose delete time has come: (id, channel, message). The caller deletes the Discord message, then
    calls `mark_deleted`."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT id, play_channel, play_message FROM market_voice WHERE status = 'opened' "
                                "AND delete_after <= NOW() ORDER BY delete_after LIMIT $1", int(limit))
    return [(int(r["id"]), r["play_channel"], r["play_message"]) for r in rows]


async def mark_deleted(voice_id: int) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE market_voice SET status = 'deleted', audio = NULL, closed_at = NOW() WHERE id = $1 AND status = 'opened'",
                           int(voice_id))


async def expire_due() -> List[Tuple[int, Optional[int], Optional[int]]]:
    """Unplayed messages past their 24 hours: audio removed. Returns (id, card channel, card message) so the cards can go too."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("UPDATE market_voice SET status = 'expired', audio = NULL, closed_at = NOW() "
                                "WHERE status = 'pending' AND expires_at <= NOW() RETURNING id, card_channel, card_message")
    return [(int(r["id"]), r["card_channel"], r["card_message"]) for r in rows]


async def purge_closed_listings() -> List[Tuple[int, Optional[int], Optional[int]]]:
    """Pending messages about a listing that is sold, cancelled or expired: audio removed."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE market_voice v SET status = 'deleted', audio = NULL, closed_at = NOW() FROM market_listings l "
            "WHERE v.listing_id = l.id AND v.status = 'pending' AND (l.status <> 'open' OR l.expires_at <= NOW()) "
            "RETURNING v.id, v.card_channel, v.card_message")
    return [(int(r["id"]), r["card_channel"], r["card_message"]) for r in rows]


async def purge_pair(owner_id: int, blocked_id: int) -> List[Tuple[int, Optional[int], Optional[int]]]:
    """The owner blocked someone: that person's pending messages to the owner are removed."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("UPDATE market_voice SET status = 'deleted', audio = NULL, closed_at = NOW() "
                                "WHERE receiver_id = $1 AND sender_id = $2 AND status = 'pending' RETURNING id, card_channel, card_message",
                                int(owner_id), int(blocked_id))
    return [(int(r["id"]), r["card_channel"], r["card_message"]) for r in rows]


async def withdraw(voice_id: int, sender_id: int) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """The sender takes a pending message back. Returns the card (channel, message) to delete, or None when it can't be."""
    p = await _pool()
    async with p.acquire() as conn:
        r = await conn.fetchrow("UPDATE market_voice SET status = 'deleted', audio = NULL, closed_at = NOW() "
                                "WHERE id = $1 AND sender_id = $2 AND status = 'pending' RETURNING card_channel, card_message",
                                int(voice_id), int(sender_id))
    return (r["card_channel"], r["card_message"]) if r else None


async def prune(days: int = KEEP_DAYS) -> int:
    """Forget old finished messages (metadata), except ones that were reported."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("DELETE FROM market_voice v WHERE v.status <> 'pending' AND v.closed_at < NOW() - $1 * INTERVAL '1 day' "
                                "AND NOT EXISTS (SELECT 1 FROM market_reports r WHERE r.voice_id = v.id) RETURNING v.id", int(days))
    return len(rows)


# ── lists and reports (metadata only: the audio column is never read here) ──

async def pending_for(receiver_id: int, limit: int = 10) -> List[Pending]:
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT id, sender_id, listing_id, duration_ms, created_at, expires_at FROM market_voice "
                                "WHERE receiver_id = $1 AND status = 'pending' AND expires_at > NOW() ORDER BY id LIMIT $2",
                                int(receiver_id), int(limit))
    return [Pending(int(r["id"]), int(r["sender_id"]), int(r["listing_id"]), int(r["duration_ms"]), r["created_at"], r["expires_at"]) for r in rows]


async def report(voice_id: int, reporter_id: int) -> str:
    """The receiver reports a message (also after it was played and deleted: only metadata is kept). ok | again | gone.
    Never touches coins or cats; the owner reads the reports."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                v = await conn.fetchrow("SELECT receiver_id, sender_id FROM market_voice WHERE id = $1", int(voice_id))
                if v is None or int(v["receiver_id"]) != int(reporter_id):
                    return "gone"
                got = await conn.fetchval("INSERT INTO market_reports (voice_id, reporter_id, reported_id) VALUES ($1, $2, $3) "
                                          "ON CONFLICT DO NOTHING RETURNING id", int(voice_id), int(reporter_id), int(v["sender_id"]))
        return "ok" if got is not None else "again"
    except Exception:
        logger.warning("[market-voice] couldn't save a report", exc_info=True)
        return "gone"
