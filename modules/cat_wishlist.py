"""Cat wishlist: post what you are looking for ("a legendary", "a rare Cuddly") so traders can see it.

Nothing is bought, reserved or traded here; it is a notice board. Limits keep it tidy: a few open wants per person
(per bot), each lasting 14 days. Notes are plain text only: no mentions, links or invites.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from modules import cat_adopt

MAX_OPEN = 3
LIFETIME_DAYS = 14
NOTE_MAX = 60
PAGE_SIZE = 8
RARITIES = tuple(name for name, _, _ in cat_adopt.RARITIES)


@dataclass(frozen=True)
class Want:
    id: int
    owner_id: int
    clone_id: Optional[int]
    rarity: str
    trait: Optional[str]
    note: str
    expires_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r) -> "Want":
        return cls(id=int(r["id"]), owner_id=int(r["owner_id"]), clone_id=r["clone_id"], rarity=r["rarity"],
                   trait=r["trait"], note=r["note"] or "", expires_at=r["expires_at"])

    def text(self) -> str:
        what = f"{cat_adopt.RARITY_EMOJI.get(self.rarity, '🐱')} a **{self.rarity}**" + (f" **{self.trait}**" if self.trait else "") + " cat"
        return what + (f" — “{self.note}”" if self.note else "")


def clean_note(raw) -> str:
    """Plain text only. Returns '' for nothing, or None when the note is not acceptable (links / invites)."""
    text = re.sub(r"[\x00-\x1f\x7f@#<>\[\]()`*_~|\\/:]", "", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    low = text.lower()
    if "http" in low or "discord.gg" in low or "www." in low or ".com" in low:
        return None
    return text[:NOTE_MAX].strip()


def valid(rarity: str, trait: Optional[str]) -> bool:
    return rarity in RARITIES and (trait is None or trait in cat_adopt.TRAITS)


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def add(owner_id: int, clone_id, rarity: str, trait: Optional[str], note: str = "") -> str:
    """ok | limit | duplicate | bad. The limit check and the insert share one transaction and a per-person lock."""
    cleaned = clean_note(note)
    if cleaned is None or not valid(rarity, trait):
        return "bad"
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", int(owner_id) % 9_000_000_000_000_000_000)
            open_n = await conn.fetchval(
                "SELECT COUNT(*) FROM cat_wishlist WHERE owner_id=$1 AND clone_key=$2 AND status='open' AND expires_at > NOW()",
                owner_id, _key(clone_id))
            if int(open_n or 0) >= MAX_OPEN:
                return "limit"
            # an expired-but-still-'open' row would block the unique index, so close those first
            await conn.execute("UPDATE cat_wishlist SET status='removed' WHERE owner_id=$1 AND clone_key=$2 AND status='open' "
                               "AND expires_at <= NOW()", owner_id, _key(clone_id))
            row = await conn.fetchval(
                "INSERT INTO cat_wishlist (owner_id, clone_id, rarity, trait, note, expires_at) "
                "VALUES ($1,$2,$3,$4,$5, NOW() + make_interval(days => $6)) ON CONFLICT DO NOTHING RETURNING id",
                owner_id, clone_id, rarity, trait, cleaned, LIFETIME_DAYS)
    return "ok" if row else "duplicate"


async def remove(want_id: int, owner_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE cat_wishlist SET status='removed' WHERE id=$1 AND owner_id=$2 AND status='open' RETURNING id",
            want_id, owner_id) is not None


async def mine(owner_id: int, clone_id) -> List[Want]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cat_wishlist WHERE owner_id=$1 AND clone_key=$2 AND status='open' AND expires_at > NOW() "
            "ORDER BY created_at DESC", owner_id, _key(clone_id))
    return [Want.from_row(r) for r in rows]


async def board(page: int = 0, size: int = PAGE_SIZE, rarity: Optional[str] = None) -> tuple:
    """(wants, total) for one page of everyone's open wants, newest first, optionally one rarity."""
    pool = await _pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM cat_wishlist WHERE status='open' AND expires_at > NOW() AND ($1::text IS NULL OR rarity=$1)", rarity)
        rows = await conn.fetch(
            "SELECT * FROM cat_wishlist WHERE status='open' AND expires_at > NOW() AND ($1::text IS NULL OR rarity=$1) "
            "ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3", rarity, size, max(0, page) * size)
    return [Want.from_row(r) for r in rows], int(total or 0)
