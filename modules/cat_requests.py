# path: modules/cat_requests.py

"""
Global Request Board, slice A (Phase 4 of the fights plan): the data layer only, no screens yet.

A player posts an open fight request: one cat, one wager from the Arena ladder, 1-24 hours. The wager is escrowed when
the request is posted (ONE transaction, rows locked) and refunded in the same transaction on cancel or expiry. Nothing
here runs a fight; accepting a request comes in a later slice. Play coins only.

Pure rules at the top (no database), queries below. The board never exposes user ids to other players: callers show a
display name and an optional server name only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from modules import cat_arena, cat_battle

logger = logging.getLogger(__name__)

MAX_OPEN = 2                    # open requests one player can have at a time
MIN_HOURS, MAX_HOURS = 1, 24    # how long a request stays on the board
DEFAULT_HOURS = 6
PAGE_SIZE = 5
BAND_SHARE = cat_arena.GAP_SHARE                # +-30% Power Rating
LOCKED_STATUSES = ("open", "accepted", "locked")     # a poster's cat is held in these states

STATUS_TEXT = {
    "ladder": cat_arena.STATUS_TEXT["ladder"],
    "new": cat_arena.STATUS_TEXT["new"],
    "me_off": cat_arena.STATUS_TEXT["me_off"],
    "cat": cat_arena.STATUS_TEXT["cat"],
    "poor": cat_arena.STATUS_TEXT["poor"],
    "limit_me": cat_arena.STATUS_TEXT["limit_me"],
    "hours": f"Pick between {MIN_HOURS} and {MAX_HOURS} hours.",
    "max_open": f"You already have {MAX_OPEN} open requests. Cancel one or wait for it to end.",
    "gone": "That request isn't open any more.",
    "not_yours": "That request isn't yours.",
    "reported": "Thanks, your report was saved. You can also block that player.",
    "self": "That's your own request. Someone else has to answer it.",
    "band": "Your cat is too far from that cat's power. Pick a closer cat.",
    "seen": "You can't answer that request.",
    "cat_you": cat_arena.STATUS_TEXT["cat"],
    "poor_you": cat_arena.STATUS_TEXT["poor"],
    "limit_them": "The poster has hit their fight limit for today, so the request was taken down.",
    "pair": cat_arena.STATUS_TEXT["pair"],
    "them_off": "The poster can no longer fight, so the request was taken down and their wager returned.",
}


# ── pure rules ──────────────────────────────────────────────────────────

def clamp_hours(hours) -> int:
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return max(MIN_HOURS, min(MAX_HOURS, h))


def hours_error(hours) -> Optional[str]:
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return "hours"
    return None if MIN_HOURS <= h <= MAX_HOURS else "hours"


def in_band(poster_power: int, my_power: int) -> bool:
    """Matchmaking only inside +-30% Power Rating."""
    return cat_battle.within_band(int(poster_power), int(my_power), BAND_SHARE)


def power_range(my_power: int) -> tuple:
    """(low, high) Power Ratings a request may have to be inside my band (symmetrical with in_band)."""
    p = max(1, int(my_power))
    return -(-p * 10 // 13), (p * 13) // 10      # ceil(p / 1.3) and floor(p * 1.3), in whole numbers


def visible_to(*, poster_id: int, viewer_id: int, poster_guild: Optional[int], viewer_guild: Optional[int],
               cross_server: bool, blocked_pairs: Sequence[int] = ()) -> bool:
    """Can this viewer see/accept this request? Never your own; never across a block (either direction);
    other servers only when the poster allowed cross-server fights."""
    if int(poster_id) == int(viewer_id) or int(poster_id) in {int(b) for b in blocked_pairs}:
        return False
    if poster_guild is not None and viewer_guild is not None and int(poster_guild) == int(viewer_guild):
        return True
    return bool(cross_server)


def board_server_label(show_server: bool, server_name: Optional[str]) -> Optional[str]:
    """The server name to show on the public board, or None (never an id)."""
    if not show_server or not server_name:
        return None
    name = " ".join(str(server_name).split())[:40]
    return name or None


def page_slice(total: int, page: int) -> tuple:
    """(page, pages, offset) with the page clamped into range."""
    pages = max(1, -(-int(total) // PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    return page, pages, page * PAGE_SIZE


# ── data ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Request:
    id: int
    poster_id: int
    cat_id: int
    wager: int
    power: int
    status: str
    clone_key: int = -1
    guild_id: Optional[int] = None
    cross_server: bool = False
    show_server: bool = False
    server_name: Optional[str] = None
    expires_at: Optional[datetime] = None
    # joined from cat_adoptions on the board
    cat_name: Optional[str] = None
    rarity: Optional[str] = None
    cat_rank: int = 0

    @classmethod
    def from_row(cls, r) -> "Request":
        def g(k, d=None):
            try:
                return r[k]
            except (KeyError, IndexError):
                return d
        return cls(id=int(r["id"]), poster_id=int(r["poster_id"]), cat_id=int(r["cat_id"]), wager=int(r["wager"]),
                   power=int(g("power", 1) or 1), status=r["status"], clone_key=int(g("clone_key", -1)),
                   guild_id=g("guild_id"), cross_server=bool(g("cross_server", False)),
                   show_server=bool(g("show_server", False)), server_name=g("server_name"), expires_at=g("expires_at"),
                   cat_name=g("cat_name"), rarity=g("rarity"), cat_rank=int(g("cat_rank", 0) or 0))


@dataclass(frozen=True)
class PostResult:
    status: str                 # ok | me_off | ladder | new | hours | cat | poor | limit_me | max_open
    request_id: int = 0
    coins: Optional[int] = None


@dataclass(frozen=True)
class EndResult:
    status: str                 # ok | gone | not_yours
    wager: int = 0


async def _pool():
    from database import get_pool
    return await get_pool()


# ── post / cancel / expire ──────────────────────────────────────────────

async def post(poster_id: int, clone_id, cat_id: int, wager: int, power: int, hours: int = DEFAULT_HOURS,
               guild_id: Optional[int] = None, guild_name: Optional[str] = None,
               cross_server: bool = False, show_server: bool = False) -> PostResult:
    """Put one request on the board and escrow the wager, all in ONE transaction. Every check is a read; the only
    writes are the escrow and the insert, so a failure part-way rolls the coins back too. `power` is the cat's Power
    Rating now (from cat_arena.my_cats), kept on the request so the band can be matched without reloading the cat."""
    if hours_error(hours):
        return PostResult("hours")
    key = cat_arena._bk(clone_id)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            me = await conn.fetchrow("SELECT arena_on, created_at FROM cat_arena WHERE owner_id = $1 AND bot_key = $2",
                                     int(poster_id), key)
            if not me or not me["arena_on"]:
                return PostResult("me_off")
            why = cat_arena.wager_error(int(wager), me["created_at"])
            if why:
                return PostResult(why)
            cat = await conn.fetchrow("SELECT id, owner_id, clone_id FROM cat_adoptions WHERE id = $1 FOR UPDATE", int(cat_id))
            if cat is None or int(cat["owner_id"]) != int(poster_id) or cat_arena._bk(cat["clone_id"]) != key:
                return PostResult("cat")
            if (await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open'", int(cat_id))
                    or await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", int(cat_id))
                    or await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'tired' AND ends_at > NOW()", int(cat_id))
                    or await conn.fetchval("SELECT 1 FROM cat_fight_requests WHERE cat_id = $1 AND status = ANY($2::text[]) "
                                           "AND (status <> 'open' OR expires_at > NOW())", int(cat_id), list(LOCKED_STATUSES))):
                return PostResult("cat")
            day = await conn.fetchrow("SELECT fights, net FROM cat_arena_daily WHERE owner_id = $1 AND day = $2",
                                      int(poster_id), cat_arena.today())
            if day and (int(day["fights"]) >= cat_arena.FIGHTS_PER_DAY or cat_arena.loss_room(int(day["net"])) < int(wager)):
                return PostResult("limit_me")
            open_now = await conn.fetchval("SELECT COUNT(*) FROM cat_fight_requests WHERE poster_id = $1 AND status = 'open' "
                                           "AND expires_at > NOW()", int(poster_id))
            if int(open_now or 0) >= MAX_OPEN:
                return PostResult("max_open")
            row = await conn.fetchrow("UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                                      "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                                      int(poster_id), -1, int(wager))
            if row is None:
                return PostResult("poor")
            rid = await conn.fetchval(
                "INSERT INTO cat_fight_requests (clone_key, guild_id, server_name, poster_id, cat_id, wager, power, "
                "cross_server, show_server, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, NOW() + $10 * INTERVAL '1 hour') "
                "RETURNING id", key, guild_id, (guild_name or None), int(poster_id), int(cat_id), int(wager), max(1, int(power)),
                bool(cross_server), bool(show_server), int(hours))
    return PostResult("ok", int(rid), int(row["coins"]))


async def cancel(request_id: int, owner_id: int) -> EndResult:
    """The poster takes an open request down: the wager comes back in the same transaction."""
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT id, poster_id, wager, status FROM cat_fight_requests WHERE id = $1 FOR UPDATE",
                                      int(request_id))
            if row is None or row["status"] != "open":
                return EndResult("gone")
            if int(row["poster_id"]) != int(owner_id):
                return EndResult("not_yours")
            await conn.execute("UPDATE cat_fight_requests SET status = 'cancelled', settled_at = NOW() WHERE id = $1", int(request_id))
            await _refund(conn, int(row["poster_id"]), int(row["wager"]))
    return EndResult("ok", int(row["wager"]))


async def _refund(conn, owner_id: int, coins: int) -> None:
    await conn.execute("UPDATE cat_wallets SET coins = coins + $3, updated_at = NOW() "
                       "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2", int(owner_id), -1, int(coins))


async def expire_due(limit: int = 50) -> int:
    """Close open requests whose time ran out and refund each poster (one transaction per request). Returns how many.
    Rows another worker holds are skipped, so two bots sweeping at once can never refund the same request twice."""
    done = 0
    p = await _pool()
    async with p.acquire() as conn:
        ids = [int(r["id"]) for r in await conn.fetch(
            "SELECT id FROM cat_fight_requests WHERE status = 'open' AND expires_at <= NOW() ORDER BY id LIMIT $1", int(limit))]
    for rid in ids:
        try:
            async with p.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("SELECT id, poster_id, wager FROM cat_fight_requests WHERE id = $1 AND status = 'open' "
                                              "AND expires_at <= NOW() FOR UPDATE SKIP LOCKED", rid)
                    if row is None:
                        continue
                    await conn.execute("UPDATE cat_fight_requests SET status = 'expired', settled_at = NOW() WHERE id = $1", rid)
                    await _refund(conn, int(row["poster_id"]), int(row["wager"]))
                    done += 1
        except Exception:
            logger.warning("[cat-requests] couldn't expire request %s", rid, exc_info=True)
    return done


# ── accepting a request ─────────────────────────────────────────────────

_HELD = ("SELECT 1 FROM cat_fight_requests WHERE cat_id = $1 AND status IN ('open','accepted','locked') AND id <> $2 "
         "AND (status <> 'open' OR expires_at > NOW())")


async def _close_refund(conn, req, status: str) -> None:
    """Take the request down and give the poster's wager back (inside the caller's transaction)."""
    await conn.execute("UPDATE cat_fight_requests SET status = $2, settled_at = NOW() WHERE id = $1 AND status = 'open'",
                       int(req["id"]), status)
    await _refund(conn, int(req["poster_id"]), int(req["wager"]))


async def accept(request_id: int, user_id: int, clone_id, cat_id: int, guild_id: Optional[int] = None,
                 seed: Optional[int] = None) -> cat_arena.AcceptResult:
    """Answer an open request with `cat_id`. ONE transaction: the request row is locked first, then cats by id, arena
    rows, daily rows and wallets by owner id. The poster's wager was escrowed when they posted; the accepter's is taken
    here, the fight runs, the winner is paid and the request is settled, or nothing at all happens. A second tap or a
    race finds the request no longer open and gets 'gone'. When the poster can no longer fight (cat gone or busy, Arena
    off, new-player cap, daily limit) the request is closed and their wager refunded."""
    import secrets
    seed = secrets.randbelow(2 ** 31) if seed is None else int(seed)
    A = cat_arena
    key = A._bk(clone_id)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow("SELECT *, (expires_at > NOW()) AS live FROM cat_fight_requests WHERE id = $1 FOR UPDATE",
                                      int(request_id))
            if req is None or req["status"] != "open" or not req["live"]:
                return A.AcceptResult("gone")
            me_id, them_id, wager = int(user_id), int(req["poster_id"]), int(req["wager"])
            if me_id == them_id:
                return A.AcceptResult("self")
            if int(req["clone_key"]) != key:
                return A.AcceptResult("gone")
            if await conn.fetchval("SELECT 1 FROM cat_blocks WHERE (owner_id = $1 AND blocked_id = $2) OR (owner_id = $2 AND blocked_id = $1)",
                                   me_id, them_id):
                return A.AcceptResult("seen")
            if not visible_to(poster_id=them_id, viewer_id=me_id, poster_guild=req["guild_id"], viewer_guild=guild_id,
                              cross_server=bool(req["cross_server"])):
                return A.AcceptResult("seen")

            cats = {int(r["id"]): r for r in await conn.fetch(
                "SELECT * FROM cat_adoptions WHERE id = ANY($1::bigint[]) ORDER BY id FOR UPDATE", [int(req["cat_id"]), int(cat_id)])}
            theirs, mine = cats.get(int(req["cat_id"])), cats.get(int(cat_id))

            async def busy(cid: int) -> bool:
                return bool(await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open'", cid)
                            or await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", cid)
                            or await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'tired' AND ends_at > NOW()", cid))

            if theirs is None or int(theirs["owner_id"]) != them_id or A._bk(theirs["clone_id"]) != key or await busy(int(req["cat_id"])):
                await _close_refund(conn, req, "cancelled")
                return A.AcceptResult("cat_them")
            if (mine is None or int(mine["owner_id"]) != me_id or A._bk(mine["clone_id"]) != key or await busy(int(cat_id))
                    or await conn.fetchval(_HELD, int(cat_id), int(request_id))):
                return A.AcceptResult("cat_you")

            arena = {int(r["owner_id"]): r for r in await conn.fetch(
                "SELECT owner_id, arena_on, rating, created_at FROM cat_arena WHERE owner_id = ANY($1::bigint[]) AND bot_key = $2 "
                "ORDER BY owner_id FOR UPDATE", [me_id, them_id], key)}
            if not arena.get(me_id) or not arena[me_id]["arena_on"]:
                return A.AcceptResult("me_off")
            if not arena.get(them_id) or not arena[them_id]["arena_on"] or A.wager_error(wager, arena[them_id]["created_at"]):
                await _close_refund(conn, req, "cancelled")
                return A.AcceptResult("them_off")
            if A.wager_error(wager, arena[me_id]["created_at"]):
                return A.AcceptResult("new")

            day = A.today()
            for uid in (me_id, them_id):
                await conn.execute("INSERT INTO cat_arena_daily (owner_id, day) VALUES ($1, $2) ON CONFLICT DO NOTHING", uid, day)
            drows = {int(r["owner_id"]): r for r in await conn.fetch(
                "SELECT owner_id, fights, net FROM cat_arena_daily WHERE owner_id = ANY($1::bigint[]) AND day = $2 ORDER BY owner_id FOR UPDATE",
                [me_id, them_id], day)}
            d_me, d_them = drows[me_id], drows[them_id]
            if int(d_me["fights"]) >= A.FIGHTS_PER_DAY or A.loss_room(int(d_me["net"])) < wager:
                return A.AcceptResult("limit_me")
            if int(d_them["fights"]) >= A.FIGHTS_PER_DAY or A.loss_room(int(d_them["net"])) < wager:
                await _close_refund(conn, req, "cancelled")
                return A.AcceptResult("limit_them")
            earlier = int(await conn.fetchval(
                "SELECT COUNT(*) FROM cat_fights WHERE status = 'settled' AND day = $3 AND "
                "((challenger_id = $1 AND challenged_id = $2) OR (challenger_id = $2 AND challenged_id = $1))", me_id, them_id, day) or 0)
            if earlier >= A.PAIR_PER_DAY:
                return A.AcceptResult("pair")

            # the power band, from the cat as it is right now (stats and Hurt included)
            cat_me, cat_them = A.cat_adopt.Cat.from_row(mine), A.cat_adopt.Cat.from_row(theirs)
            hurt = {c.id: bool(await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'hurt' AND ends_at > NOW()", c.id))
                    for c in (cat_me, cat_them)}
            f_me = A.cat_battle.from_cat(cat_me, await A.cat_stats._load(conn, cat_me.id), hurt=hurt[cat_me.id])
            f_them = A.cat_battle.from_cat(cat_them, await A.cat_stats._load(conn, cat_them.id), hurt=hurt[cat_them.id])
            if not in_band(f_them.power, f_me.power):
                return A.AcceptResult("band")

            # escrow the accepter's wager (the poster's is already held)
            wallets = {int(r["owner_id"]): int(r["coins"]) for r in await conn.fetch(
                "SELECT owner_id, coins FROM cat_wallets WHERE owner_id = ANY($1::bigint[]) AND COALESCE(clone_id, -1) = $2 "
                "ORDER BY owner_id FOR UPDATE", [me_id, them_id], -1)}
            if wallets.get(me_id, 0) < wager:
                return A.AcceptResult("poor", wager=wager)
            took = await conn.fetchval("UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                                       "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins", me_id, -1, wager)
            if took is None:
                raise A._Abort("poor", wager=wager)

            frow = await conn.fetchrow(
                "INSERT INTO cat_fights (mode, clone_key, guild_id, challenger_id, challenged_id, challenger_cat, wager, status, expires_at) "
                "VALUES ('request', $1, $2, $3, $4, $5, $6, 'pending', NOW()) RETURNING *",
                key, req["guild_id"], them_id, me_id, int(req["cat_id"]), wager)
            fight = A.Fight.from_row(frow)
            res = await A._fight_and_settle(conn, fight, me_id, them_id, int(cat_id), mine, theirs, arena, day, earlier, key, seed)
            await conn.execute("UPDATE cat_fight_requests SET status = 'settled', accepter_id = $2, fight_id = $3, settled_at = NOW() WHERE id = $1",
                               int(request_id), me_id, fight.id)
            return res


# ── reports and repeated pairs ──────────────────────────────────────────

REPEAT_DAYS, REPEAT_FLAG_AT = 7, 6      # this many settled fights between the same two players in this many days get flagged


async def report(fight_id: int, reporter_id: int) -> str:
    """Save a report about the OTHER player of a fight the reporter took part in. One per person per fight.
    Returns ok | again | gone. Never touches coins or cats; the owner reads the reports."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                f = await conn.fetchrow("SELECT challenger_id, challenged_id FROM cat_fights WHERE id = $1", int(fight_id))
                if f is None or int(reporter_id) not in (int(f["challenger_id"]), int(f["challenged_id"])):
                    return "gone"
                other = int(f["challenged_id"]) if int(f["challenger_id"]) == int(reporter_id) else int(f["challenger_id"])
                got = await conn.fetchval("INSERT INTO cat_fight_reports (fight_id, reporter_id, reported_id) VALUES ($1, $2, $3) "
                                          "ON CONFLICT DO NOTHING RETURNING id", int(fight_id), int(reporter_id), other)
        return "ok" if got is not None else "again"
    except Exception:
        logger.warning("[cat-requests] couldn't save a report", exc_info=True)
        return "gone"


async def pair_recent(a: int, b: int, days: int = REPEAT_DAYS) -> int:
    """Settled fights between two players in the last `days` days. Never raises (0 on error)."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM cat_fights WHERE status = 'settled' AND settled_at > NOW() - $3 * INTERVAL '1 day' AND "
                "((challenger_id = $1 AND challenged_id = $2) OR (challenger_id = $2 AND challenged_id = $1))",
                int(a), int(b), int(days)) or 0)
    except Exception:
        logger.debug("[cat-requests] couldn't count a pair", exc_info=True)
        return 0


def pair_flag_text(a: int, b: int, n: int) -> Optional[str]:
    """The note for the owner when two players keep fighting each other (possible coin passing). None below the line."""
    if n < REPEAT_FLAG_AT:
        return None
    lo, hi = sorted((int(a), int(b)))
    return f"Cat fights: players {lo} and {hi} have fought each other {n} times in {REPEAT_DAYS} days. Worth a look for coin passing."


# ── reading the board ───────────────────────────────────────────────────

async def locked_cat_ids(cat_ids: Sequence[int]) -> set:
    """Cats held by a request that is open, accepted or locked (they can't be sold, traded, released or sent to fight).
    Never raises (on error: nothing locked, so a screen still opens)."""
    ids = [int(i) for i in cat_ids]
    if not ids:
        return set()
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT cat_id FROM cat_fight_requests WHERE cat_id = ANY($1::bigint[]) "
                                    "AND status = ANY($2::text[]) AND (status <> 'open' OR expires_at > NOW())",
                                    ids, list(LOCKED_STATUSES))
        return {int(r["cat_id"]) for r in rows}
    except Exception:
        logger.debug("[cat-requests] couldn't read locked cats", exc_info=True)
        return set()


async def mine(owner_id: int, clone_id) -> List[Request]:
    """This player's open requests, newest first. Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch(_SELECT + "WHERE r.poster_id = $1 AND r.clone_key = $2 AND r.status = 'open' "
                                    "AND r.expires_at > NOW() ORDER BY r.id DESC LIMIT 10", int(owner_id), cat_arena._bk(clone_id))
        return [Request.from_row(r) for r in rows]
    except Exception:
        logger.debug("[cat-requests] couldn't list my requests", exc_info=True)
        return []


_SELECT = ("SELECT r.id, r.clone_key, r.guild_id, r.server_name, r.poster_id, r.cat_id, r.wager, r.power, r.cross_server, "
           "r.show_server, r.status, r.expires_at, a.name AS cat_name, a.rarity AS rarity, a.cat_rank AS cat_rank "
           "FROM cat_fight_requests r JOIN cat_adoptions a ON a.id = r.cat_id ")


async def board(viewer_id: int, clone_id, viewer_guild: Optional[int], my_power: int, page: int = 0):
    """(requests on this page, page, pages) the viewer may accept: open, not theirs, not across a block (either way),
    this server or cross-server-allowed, inside the +-30% Power band. Newest first. Never raises."""
    lo, hi = power_range(my_power)
    where = ("WHERE r.status = 'open' AND r.expires_at > NOW() AND r.clone_key = $1 AND r.poster_id <> $2 "
             "AND r.power BETWEEN $3 AND $4 AND (r.cross_server OR ($5::bigint IS NOT NULL AND r.guild_id = $5)) "
             "AND NOT EXISTS (SELECT 1 FROM cat_blocks b WHERE (b.owner_id = $2 AND b.blocked_id = r.poster_id) "
             "OR (b.owner_id = r.poster_id AND b.blocked_id = $2)) ")
    args = (cat_arena._bk(clone_id), int(viewer_id), lo, hi, viewer_guild)
    try:
        p = await _pool()
        async with p.acquire() as conn:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM cat_fight_requests r " + where, *args) or 0)
            page, pages, offset = page_slice(total, page)
            rows = await conn.fetch(_SELECT + where + "ORDER BY r.id DESC LIMIT $6 OFFSET $7", *args, PAGE_SIZE, offset)
        return [Request.from_row(r) for r in rows], page, pages
    except Exception:
        logger.debug("[cat-requests] couldn't read the board", exc_info=True)
        return [], 0, 1


# ── block list ──────────────────────────────────────────────────────────

async def block(owner_id: int, blocked_id: int) -> bool:
    """True when the block is now in place. You can't block yourself."""
    if int(owner_id) == int(blocked_id):
        return False
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("INSERT INTO cat_blocks (owner_id, blocked_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                           int(owner_id), int(blocked_id))
    return True


async def unblock(owner_id: int, blocked_id: int) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM cat_blocks WHERE owner_id = $1 AND blocked_id = $2", int(owner_id), int(blocked_id))


async def blocked_by(owner_id: int) -> List[int]:
    """Ids this player blocked (for their own block-list screen only). Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT blocked_id FROM cat_blocks WHERE owner_id = $1 ORDER BY created_at DESC LIMIT 50", int(owner_id))
        return [int(r["blocked_id"]) for r in rows]
    except Exception:
        logger.debug("[cat-requests] couldn't read the block list", exc_info=True)
        return []
