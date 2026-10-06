"""Creature detail, lock, nickname and buddy (Phase 3: P3-04, P3-05).

Every function is scoped by ``(owned_id, user_id, clone_key)`` in SQL, so an id taken from
a select value or a stale message can never read or change someone else's creature.

* ``load_creature`` is read-only.
* ``toggle_lock`` and ``set_nickname`` are single guarded UPDATEs.
* ``toggle_buddy`` runs in one transaction: it locks the creature row, then writes
  ``catch_players.buddy_id`` and an audit row. A buddy whose creature was later sold or
  evolved is handled by readers (a buddy id that no longer matches a creature reads as "no
  buddy"), so nothing here has to chase deletes.
* Nicknames are cleaned by ``clean_nickname`` (length, control characters, mentions, links
  and the bundled banned-word list) BEFORE the database sees them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from modules import catch_db
from modules.catch_game import SOURCE_BY_CODE, compute_stats, iv_percent
from modules.catch_species import all_species

NICKNAME_MAX = 24
BANNED_WORDS_PATH = Path(__file__).resolve().parent.parent / "data" / "preset_banned_words.txt"
_FORBIDDEN_CHARS = frozenset("@<>`\\")
_LINK_HINTS = ("http", "www.", "discord.gg", ".gg/", "://")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s"})
_WORD_SUFFIX = r"(?:s|es|ed|ing|er|ers)?"

_banned_re: re.Pattern[str] | None = None
_banned_loaded = False


@dataclass(frozen=True, slots=True)
class CreatureDetail:
    id: int
    species_id: int
    name: str
    rarity: str
    element: str
    element2: str | None
    habitat: str
    level: int
    xp: int
    nickname: str | None
    shiny: bool
    special: bool
    favorite: bool
    locked: bool
    is_buddy: bool
    stats: dict[str, int]
    iv_percent: float
    source: str
    caught_at: datetime | None
    caught_in_guild: int | None

    @property
    def display_name(self) -> str:
        return self.nickname or self.name


@dataclass(frozen=True, slots=True)
class NicknameCheck:
    ok: bool
    value: str | None = None  # cleaned nickname, or None to clear it
    reason: str | None = None  # too_long, invalid_chars, link, blocked


@dataclass(frozen=True, slots=True)
class ChangeResult:
    ok: bool
    reason: str | None = None  # not_found, no_player, unknown_species, invalid
    value: object = None  # the new lock flag / nickname / buddy flag


def _load_banned() -> re.Pattern[str] | None:
    global _banned_re, _banned_loaded
    if _banned_loaded:
        return _banned_re
    words: set[str] = set()
    try:
        for line in BANNED_WORDS_PATH.read_text(encoding="utf-8").splitlines():
            word = line.strip().lower()
            # Only plain words and phrases: the file also holds symbol-heavy entries that
            # a whole-word match cannot use safely.
            if len(word) >= 3 and re.fullmatch(r"[a-z0-9 ]+", word):
                words.add(word)
    except OSError:
        words = set()
    parts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    _banned_re = (
        re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")" + _WORD_SUFFIX + r"(?!\w)", re.IGNORECASE)
        if parts else None
    )
    _banned_loaded = True
    return _banned_re


def contains_banned_word(value: str) -> bool:
    pattern = _load_banned()
    if pattern is None:
        return False
    lowered = value.lower()
    return bool(pattern.search(lowered) or pattern.search(lowered.translate(_LEET)))


def clean_nickname(raw: str | None) -> NicknameCheck:
    """Validate a nickname typed by a player. Blank clears the nickname."""
    collapsed = " ".join((raw or "").split())  # also folds odd Unicode spaces
    if not collapsed:
        return NicknameCheck(True, None)
    if len(collapsed) > NICKNAME_MAX:
        return NicknameCheck(False, reason="too_long")
    for ch in collapsed:
        if ch in _FORBIDDEN_CHARS or unicodedata.category(ch)[0] == "C":
            return NicknameCheck(False, reason="invalid_chars")
    lowered = collapsed.lower()
    if any(hint in lowered for hint in _LINK_HINTS):
        return NicknameCheck(False, reason="link")
    if contains_banned_word(collapsed):
        return NicknameCheck(False, reason="blocked")
    return NicknameCheck(True, collapsed)


async def load_creature(owned_id: int, user_id: int, clone_id: int | None, *, conn=None) -> CreatureDetail | None:
    """One creature of this player, or None. Read-only."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        row = await db.fetchrow(
            "SELECT o.id, o.species_id, o.level, o.xp, o.nickname, o.shiny, o.special, o.favorite, "
            "o.locked, o.ivs, o.source, o.caught_at, o.caught_in_guild, "
            "(p.buddy_id IS NOT NULL AND p.buddy_id = o.id) AS is_buddy "
            "FROM catch_owned o LEFT JOIN catch_players p ON p.user_id = o.user_id AND p.clone_key = o.clone_key "
            "WHERE o.id = $1 AND o.user_id = $2 AND o.clone_key = $3",
            owned_id, user_id, key,
        )
    if row is None:
        return None
    species = all_species().get(int(row["species_id"]))
    if species is None:
        return None
    ivs = [int(v) for v in row["ivs"]]
    level = int(row["level"])
    return CreatureDetail(
        id=int(row["id"]), species_id=int(row["species_id"]), name=str(species["name"]),
        rarity=str(species["rarity"]), element=str(species["element"]),
        element2=(str(species["element2"]) if species.get("element2") else None),
        habitat=str(species["habitat"]), level=level, xp=int(row["xp"]), nickname=row["nickname"],
        shiny=bool(row["shiny"]), special=bool(row["special"]), favorite=bool(row["favorite"]),
        locked=bool(row["locked"]), is_buddy=bool(row["is_buddy"]),
        stats=compute_stats([int(v) for v in species["base_stats"]], ivs, level),
        iv_percent=iv_percent(ivs), source=SOURCE_BY_CODE.get(int(row["source"]), "wild"),
        caught_at=row["caught_at"], caught_in_guild=row["caught_in_guild"],
    )


async def toggle_lock(owned_id: int, user_id: int, clone_id: int | None, *, conn=None) -> ChangeResult:
    """Flip the lock flag (a locked creature cannot be sold). One guarded UPDATE."""
    async with catch_db.transaction(conn) as db:
        value = await db.fetchval(
            "UPDATE catch_owned SET locked = NOT locked "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 RETURNING locked",
            owned_id, user_id, catch_db.clone_key(clone_id),
        )
    if value is None:
        return ChangeResult(False, "not_found")
    return ChangeResult(True, value=bool(value))


async def set_nickname(owned_id: int, user_id: int, clone_id: int | None, raw: str | None, *, conn=None) -> ChangeResult:
    """Validate, then store (or clear) a nickname on one of the player's creatures."""
    check = clean_nickname(raw)
    if not check.ok:
        return ChangeResult(False, check.reason or "invalid")
    async with catch_db.transaction(conn) as db:
        updated = await db.fetchval(
            "UPDATE catch_owned SET nickname = $4 "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 RETURNING id",
            owned_id, user_id, catch_db.clone_key(clone_id), check.value,
        )
    if updated is None:
        return ChangeResult(False, "not_found")
    return ChangeResult(True, value=check.value)


async def toggle_buddy(
    owned_id: int, user_id: int, clone_id: int | None, *, guild_id: int | None = None, conn=None,
) -> ChangeResult:
    """Make this creature the buddy, or clear the buddy if it already is. Audited."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.transaction(conn) as db:
        owned = await db.fetchval(
            "SELECT id FROM catch_owned WHERE id = $1 AND user_id = $2 AND clone_key = $3 FOR UPDATE",
            owned_id, user_id, key,
        )
        if owned is None:
            return ChangeResult(False, "not_found")
        current = await db.fetchval(
            "SELECT buddy_id FROM catch_players WHERE user_id = $1 AND clone_key = $2 FOR UPDATE",
            user_id, key,
        )
        # fetchval cannot tell "no player row" from "buddy_id is NULL", so check the row too.
        exists = await db.fetchval(
            "SELECT 1 FROM catch_players WHERE user_id = $1 AND clone_key = $2", user_id, key,
        )
        if exists is None:
            return ChangeResult(False, "no_player")
        becoming = None if (current is not None and int(current) == owned_id) else owned_id
        await db.execute(
            "UPDATE catch_players SET buddy_id = $3, updated_at = now() WHERE user_id = $1 AND clone_key = $2",
            user_id, key, becoming,
        )
        await db.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, ref_id, detail) "
            "VALUES ($1, $1, $2, $3, 'buddy', $4, $5::jsonb)",
            user_id, clone_id, guild_id, owned_id, json.dumps({"buddy": becoming is not None}),
        )
    return ChangeResult(True, value=becoming is not None)


__all__ = [
    "NICKNAME_MAX", "ChangeResult", "CreatureDetail", "NicknameCheck", "clean_nickname",
    "contains_banned_word", "load_creature", "set_nickname", "toggle_buddy", "toggle_lock",
]
