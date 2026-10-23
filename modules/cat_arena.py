# path: modules/cat_arena.py

"""
Cat Arena: opt-in fights between players (play coins only, never real money). Phase 3 of the fights plan.

Nothing happens without an accept. A player turns Arena on, then challenges another Arena player (same server). The
challenged player sees the odds band and taps Accept or Decline (10 minutes to answer). On ACCEPT both wagers are
escrowed, the fight runs and is settled, all in ONE database transaction with every row locked, so a double tap, a
double accept or a crash can never take coins twice or leave a half-finished fight. Winner gets both wagers minus the
8% fee (a coin sink). The loser's cat becomes Tired (and Hurt after a knock-out). The loser always keeps their cat.

Pure rules at the top (no database), queries below.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from modules import cat_adopt, cat_battle, cat_records, cat_stats

logger = logging.getLogger(__name__)

LADDER = (500, 2_500, 10_000, 50_000)           # allowed wagers (coins)
FEE_PERCENT = 8                                 # of the whole pot; a coin sink
FIGHTS_PER_DAY = 10                             # fights a person can finish in a day (UTC), as challenger or challenged
DAILY_NET_LOSS_LIMIT = 100_000                  # coins a person can lose to fights in a day
PAIR_PER_DAY = 3                                # the same two people can fight at most this often a day
PAIR_GAIN = (1.0, 0.5, 0.25)                    # rating gain shrinks on every repeat between the same pair
EXPIRE_MINUTES = 10                             # a challenge not answered by then is gone
MAX_PENDING_OUT = 3                             # open challenges one person can have out at a time
TIRED_HOURS, HURT_HOURS = 2, 6                  # the loser's cat is Tired; after a knock-out also Hurt (-10% stats)
NEW_ARENA_DAYS, NEW_MAX_WAGER = 3, 2_500        # a brand-new Arena player has a wager cap for a few days
START_RATING, RATING_FLOOR, ELO_K = 1000, 100, 32
GAP_SHARE = 0.30                                # matching band: +-30% Power Rating

BOT_ALL = -1


def _bk(clone_id) -> int:
    return BOT_ALL if clone_id is None else int(clone_id)


def today() -> date:
    return datetime.now(timezone.utc).date()


# ── pure rules ──────────────────────────────────────────────────────────

def fee_for(wager: int) -> int:
    """The coins the game keeps from a pot of two wagers (rounded down)."""
    return (2 * int(wager) * FEE_PERCENT) // 100


def payout_for(wager: int) -> int:
    return 2 * int(wager) - fee_for(wager)


def wager_error(wager: int, joined_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
    """None when this wager is allowed for this player, else 'ladder' or 'new' (new Arena players have a lower cap)."""
    if wager not in LADDER:
        return "ladder"
    now = now or datetime.now(timezone.utc)
    if joined_at is not None and now - joined_at < timedelta(days=NEW_ARENA_DAYS) and wager > NEW_MAX_WAGER:
        return "new"
    return None


def pair_scale(earlier_today: int) -> float:
    """How much of the rating change counts when the same two people already fought `earlier_today` times today."""
    return PAIR_GAIN[min(max(0, int(earlier_today)), len(PAIR_GAIN) - 1)]


def rating_change(winner_rating: int, loser_rating: int, earlier_today: int = 0, shield_loser: bool = False) -> Tuple[int, int]:
    """(winner_delta, loser_delta). Elo, K=32. The weaker side of a mismatch (a rating shield) loses nothing."""
    expected = 1.0 / (1.0 + 10 ** ((loser_rating - winner_rating) / 400.0))
    gain = max(1, int(round(ELO_K * (1.0 - expected) * pair_scale(earlier_today))))
    loss = 0 if shield_loser else -gain
    if loser_rating + loss < RATING_FLOOR:
        loss = min(0, RATING_FLOOR - loser_rating)
    return gain, loss


def mismatched(p1: int, p2: int) -> bool:
    """True when the two Power Ratings are further apart than the +-30% matching band (the card warns, the weaker side is shielded)."""
    return not cat_battle.within_band(p1, p2, GAP_SHARE)


def loss_room(net_today: int) -> int:
    """How many more coins this person may lose to fights today."""
    return max(0, DAILY_NET_LOSS_LIMIT + min(0, int(net_today)))


def odds_for_defender(attacker: cat_battle.Fighter, defender: cat_battle.Fighter, seed: int = 1) -> Tuple[str, str]:
    """The odds band from the DEFENDER's side (the person who accepts is side b)."""
    share = 1.0 - cat_battle.win_share(attacker, defender, seed)
    return cat_battle.band(share)


@dataclass(frozen=True)
class Effects:
    tired_until: Optional[datetime] = None
    hurt_until: Optional[datetime] = None

    def tired(self, now: Optional[datetime] = None) -> bool:
        return bool(self.tired_until and self.tired_until > (now or datetime.now(timezone.utc)))

    def hurt(self, now: Optional[datetime] = None) -> bool:
        return bool(self.hurt_until and self.hurt_until > (now or datetime.now(timezone.utc)))


def effect_text(fx: Effects, now: Optional[datetime] = None) -> str:
    bits = []
    if fx.tired(now):
        bits.append(f"😴 Tired until <t:{int(fx.tired_until.timestamp())}:t>")
    if fx.hurt(now):
        bits.append(f"🩹 Hurt until <t:{int(fx.hurt_until.timestamp())}:t> (-10% stats)")
    return " · ".join(bits)


STATUS_TEXT = {
    "ladder": f"Pick one of the wagers: {', '.join(f'{w:,}' for w in LADDER)} coins.",
    "new": f"New Arena players can wager up to {NEW_MAX_WAGER:,} coins for their first {NEW_ARENA_DAYS} days.",
    "self": "You can't challenge yourself.",
    "off": "Both players need **Arena** turned on.",
    "me_off": "Turn **Arena** on first.",
    "them_off": "That player hasn't turned **Arena** on.",
    "cat": "That cat can't fight right now (it's for sale, out, tired, or isn't yours).",
    "cat_them": "The challenger's cat can't fight any more, so this challenge was cancelled.",
    "cat_you": "That cat can't fight right now (for sale, out, or tired). Pick another cat.",
    "no_cat": "You don't have a cat that is ready to fight.",
    "poor": "You need more coins for that wager.",
    "poor_them": "The challenger doesn't have the coins any more, so this challenge was cancelled.",
    "limit_me": f"You've reached today's limit ({FIGHTS_PER_DAY} fights, or {DAILY_NET_LOSS_LIMIT:,} coins lost). It resets at UTC midnight.",
    "limit_them": "That player has reached today's fight limit.",
    "pair": f"The same two players can fight {PAIR_PER_DAY} times a day. Try again tomorrow.",
    "pending": f"You already have {MAX_PENDING_OUT} challenges waiting, or one to this player. Wait or cancel one.",
    "gone": "That challenge isn't open any more.",
    "not_yours": "That challenge isn't yours to answer.",
    "wager_new": f"New Arena players can wager up to {NEW_MAX_WAGER:,} coins for their first {NEW_ARENA_DAYS} days.",
}


# ── data ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArenaState:
    on: bool = False
    rating: int = START_RATING
    wins: int = 0
    losses: int = 0
    fights_today: int = 0
    net_today: int = 0
    joined_at: Optional[datetime] = None


@dataclass(frozen=True)
class Fight:
    id: int
    challenger_id: int
    challenged_id: int
    challenger_cat: int
    wager: int
    status: str
    clone_key: int = BOT_ALL
    guild_id: Optional[int] = None
    challenged_cat: Optional[int] = None
    fee: int = 0
    seed: Optional[int] = None
    winner_id: Optional[int] = None
    ended_by: Optional[str] = None
    snap: Optional[str] = None
    expires_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, r) -> "Fight":
        def g(k, d=None):
            try:
                return r[k]
            except (KeyError, IndexError):
                return d
        return cls(id=int(r["id"]), challenger_id=int(r["challenger_id"]), challenged_id=int(r["challenged_id"]),
                   challenger_cat=int(r["challenger_cat"]), wager=int(r["wager"]), status=r["status"],
                   clone_key=int(g("clone_key", BOT_ALL)), guild_id=g("guild_id"), challenged_cat=g("challenged_cat"),
                   fee=int(g("fee", 0) or 0), seed=g("seed"), winner_id=g("winner_id"), ended_by=g("ended_by"),
                   snap=g("snap"), expires_at=g("expires_at"), settled_at=g("settled_at"))


@dataclass(frozen=True)
class Ready:
    """A cat that is allowed to fight now, with everything the screens need."""
    cat: cat_adopt.Cat
    stats: cat_stats.Stats
    fx: Effects
    fighter: cat_battle.Fighter


@dataclass(frozen=True)
class ChallengeResult:
    status: str                 # ok | self | off | me_off | them_off | ladder | new | cat | poor | limit_me | pending
    fight_id: int = 0


@dataclass(frozen=True)
class AcceptResult:
    status: str                 # ok | gone | not_yours | me_off | cat_you | cat_them | poor | poor_them | limit_me | limit_them | pair | new
    fight: Optional[Fight] = None
    result: Optional[cat_battle.BattleResult] = None
    a: Optional[cat_battle.Fighter] = None          # challenger's cat (side a)
    b: Optional[cat_battle.Fighter] = None          # the person who accepted (side b)
    winner_id: int = 0
    loser_id: int = 0
    wager: int = 0
    fee: int = 0
    payout: int = 0
    rating: Dict[int, Tuple[int, int]] = field(default_factory=dict)    # user id -> (new rating, change)
    hurt: bool = False
    mismatch: bool = False


class _Abort(Exception):
    """Raised inside the transaction to roll everything back and report why."""

    def __init__(self, status: str, **kw):
        super().__init__(status)
        self.status, self.kw = status, kw


async def _pool():
    from database import get_pool
    return await get_pool()


# ── arena state ─────────────────────────────────────────────────────────

async def get_state(owner_id: int, clone_id) -> ArenaState:
    """Never raises: a database problem shows 'Arena off' instead of breaking the menu."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            row = await conn.fetchrow("SELECT arena_on, rating, wins, losses, created_at FROM cat_arena "
                                      "WHERE owner_id = $1 AND bot_key = $2", owner_id, _bk(clone_id))
            day = await conn.fetchrow("SELECT fights, net FROM cat_arena_daily WHERE owner_id = $1 AND day = $2",
                                      owner_id, today())
        return ArenaState(bool(row["arena_on"]) if row else False, int(row["rating"]) if row else START_RATING,
                          int(row["wins"]) if row else 0, int(row["losses"]) if row else 0,
                          int(day["fights"]) if day else 0, int(day["net"]) if day else 0,
                          row["created_at"] if row else None)
    except Exception:
        logger.debug("[cat-arena] couldn't read the arena state", exc_info=True)
        return ArenaState()


async def set_on(owner_id: int, clone_id, on: bool) -> ArenaState:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO cat_arena (owner_id, bot_key, arena_on) VALUES ($1, $2, $3) "
            "ON CONFLICT (owner_id, bot_key) DO UPDATE SET arena_on = EXCLUDED.arena_on, updated_at = NOW()",
            owner_id, _bk(clone_id), bool(on))
    return await get_state(owner_id, clone_id)


# ── cats that can fight ─────────────────────────────────────────────────

async def effects_for(cat_ids: Sequence[int]) -> Dict[int, Effects]:
    ids = [int(i) for i in cat_ids]
    out: Dict[int, Effects] = {i: Effects() for i in ids}
    if not ids:
        return out
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT cat_id, effect_key, ends_at FROM cat_effects "
                                    "WHERE cat_id = ANY($1::bigint[]) AND ends_at > NOW()", ids)
        for r in rows:
            cur = out[int(r["cat_id"])]
            if r["effect_key"] == "tired":
                out[int(r["cat_id"])] = Effects(r["ends_at"], cur.hurt_until)
            elif r["effect_key"] == "hurt":
                out[int(r["cat_id"])] = Effects(cur.tired_until, r["ends_at"])
    except Exception:
        logger.debug("[cat-arena] couldn't read effects", exc_info=True)
    return out


async def busy_ids(cat_ids: Sequence[int]) -> set:
    """Cats that are for sale, out on a walk/heist, or held by a board request (they can't fight)."""
    ids = [int(i) for i in cat_ids]
    if not ids:
        return set()
    p = await _pool()
    async with p.acquire() as conn:
        a = await conn.fetch("SELECT adoption_id FROM cat_listings WHERE adoption_id = ANY($1::bigint[]) AND status = 'open'", ids)
        a2 = await conn.fetch("SELECT item_ref FROM market_listings WHERE item_type = 'cat' AND item_ref = ANY($1::bigint[]) AND status = 'open'", ids)
        b = await conn.fetch("SELECT adoption_id FROM cat_outings WHERE adoption_id = ANY($1::bigint[]) AND claimed_at IS NULL", ids)
        c = await conn.fetch("SELECT cat_id FROM cat_fight_requests WHERE cat_id = ANY($1::bigint[]) AND status = ANY($2::text[]) "
                             "AND (status <> 'open' OR expires_at > NOW())", ids, ["open", "accepted", "locked"])
    return {int(r["adoption_id"]) for r in a} | {int(r["item_ref"]) for r in a2} | {int(r["adoption_id"]) for r in b} | {int(r["cat_id"]) for r in c}


async def my_cats(owner_id: int, clone_id) -> List[Ready]:
    """This person's cats with stats and effects, strongest first. `Ready.fx` says Tired/Hurt; callers decide what to show."""
    cats = await cat_adopt.list_cats(owner_id, clone_id)
    ids = [c.id for c in cats]
    stats = await cat_stats.get_many(ids)
    fx = await effects_for(ids)
    out = []
    vets = await cat_records.bonuses(ids)
    for c in cats:
        f = cat_battle.from_cat(c, stats[c.id], hurt=fx[c.id].hurt(), vet_pct=vets.get(c.id, 0))
        out.append(Ready(c, stats[c.id], fx[c.id], f))
    out.sort(key=lambda r: -r.fighter.power)
    return out


async def ready_cats(owner_id: int, clone_id) -> List[Ready]:
    """Cats that can fight right now (not tired, not for sale, not out), strongest first."""
    allc = await my_cats(owner_id, clone_id)
    busy = await busy_ids([r.cat.id for r in allc])
    return [r for r in allc if r.cat.id not in busy and not r.fx.tired()]


# ── challenges ──────────────────────────────────────────────────────────

async def send_challenge(challenger_id: int, clone_id, target_id: int, cat_id: int, wager: int,
                         guild_id: Optional[int] = None) -> ChallengeResult:
    """Create a pending challenge. No coins move yet (escrow happens on accept)."""
    if int(challenger_id) == int(target_id):
        return ChallengeResult("self")
    key = _bk(clone_id)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT owner_id, arena_on, created_at FROM cat_arena "
                                    "WHERE owner_id = ANY($1::bigint[]) AND bot_key = $2 ORDER BY owner_id",
                                    [int(challenger_id), int(target_id)], key)
            by = {int(r["owner_id"]): r for r in rows}
            me, them = by.get(int(challenger_id)), by.get(int(target_id))
            if not me or not me["arena_on"]:
                return ChallengeResult("me_off")
            if not them or not them["arena_on"]:
                return ChallengeResult("them_off")
            why = wager_error(int(wager), me["created_at"])
            if why:
                return ChallengeResult(why)
            cat = await conn.fetchrow("SELECT id, owner_id, clone_id FROM cat_adoptions WHERE id = $1 FOR UPDATE", int(cat_id))
            if cat is None or int(cat["owner_id"]) != int(challenger_id) or _bk(cat["clone_id"]) != key:
                return ChallengeResult("cat")
            if (await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open' UNION ALL SELECT 1 FROM market_listings WHERE item_type = 'cat' AND item_ref = $1 AND status = 'open'", int(cat_id))
                    or await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", int(cat_id))
                    or await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'tired' AND ends_at > NOW()", int(cat_id))):
                return ChallengeResult("cat")
            coins = await conn.fetchval("SELECT coins FROM cat_wallets WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                                        int(challenger_id), BOT_ALL)
            if int(coins or 0) < int(wager):
                return ChallengeResult("poor")
            day = await conn.fetchrow("SELECT fights, net FROM cat_arena_daily WHERE owner_id = $1 AND day = $2", int(challenger_id), today())
            if day and (int(day["fights"]) >= FIGHTS_PER_DAY or loss_room(int(day["net"])) < int(wager)):
                return ChallengeResult("limit_me")
            await conn.execute("UPDATE cat_fights SET status = 'expired' WHERE status = 'pending' AND expires_at <= NOW() "
                               "AND (challenger_id = $1 OR challenged_id = $1)", int(challenger_id))
            open_out = await conn.fetchval("SELECT COUNT(*) FROM cat_fights WHERE challenger_id = $1 AND status = 'pending'", int(challenger_id))
            same = await conn.fetchval("SELECT 1 FROM cat_fights WHERE challenger_id = $1 AND challenged_id = $2 AND status = 'pending'",
                                       int(challenger_id), int(target_id))
            if int(open_out or 0) >= MAX_PENDING_OUT or same:
                return ChallengeResult("pending")
            fid = await conn.fetchval(
                "INSERT INTO cat_fights (clone_key, guild_id, challenger_id, challenged_id, challenger_cat, wager, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, NOW() + $7 * INTERVAL '1 minute') RETURNING id",
                key, guild_id, int(challenger_id), int(target_id), int(cat_id), int(wager), EXPIRE_MINUTES)
    return ChallengeResult("ok", int(fid))


async def get_fight(fight_id: int) -> Optional[Fight]:
    p = await _pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM cat_fights WHERE id = $1", int(fight_id))
    return Fight.from_row(row) if row else None


async def incoming(owner_id: int, clone_id) -> List[Fight]:
    """Open challenges waiting for this person's answer, oldest first. Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM cat_fights WHERE challenged_id = $1 AND clone_key = $2 "
                                    "AND status = 'pending' AND expires_at > NOW() ORDER BY id LIMIT 10",
                                    int(owner_id), _bk(clone_id))
        return [Fight.from_row(r) for r in rows]
    except Exception:
        logger.debug("[cat-arena] couldn't list incoming challenges", exc_info=True)
        return []


async def outgoing_count(owner_id: int, clone_id) -> int:
    try:
        p = await _pool()
        async with p.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM cat_fights WHERE challenger_id = $1 AND clone_key = $2 "
                                           "AND status = 'pending' AND expires_at > NOW()", int(owner_id), _bk(clone_id)) or 0)
    except Exception:
        logger.warning("swallowed exception in outgoing_count", exc_info=True)
        return 0


async def recent(owner_id: int, clone_id, limit: int = 3) -> List[Fight]:
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM cat_fights WHERE (challenger_id = $1 OR challenged_id = $1) AND clone_key = $2 "
                                    "AND status = 'settled' ORDER BY id DESC LIMIT $3", int(owner_id), _bk(clone_id), int(limit))
        return [Fight.from_row(r) for r in rows]
    except Exception:
        logger.warning("swallowed exception in recent", exc_info=True)
        return []


async def _close(fight_id: int, user_col: str, user_id: int, new_status: str) -> bool:
    p = await _pool()
    async with p.acquire() as conn:
        got = await conn.fetchval(f"UPDATE cat_fights SET status = $3 WHERE id = $1 AND {user_col} = $2 AND status = 'pending' RETURNING id",
                                  int(fight_id), int(user_id), new_status)
    return got is not None


async def decline(fight_id: int, user_id: int) -> bool:
    """The challenged person says no (no penalty). True when it was still open."""
    return await _close(fight_id, "challenged_id", user_id, "declined")


async def cancel(fight_id: int, user_id: int) -> bool:
    """The challenger takes their challenge back."""
    return await _close(fight_id, "challenger_id", user_id, "cancelled")


# ── accept: escrow, fight and settle in ONE transaction ─────────────────

def _snap(a: cat_battle.Fighter, b: cat_battle.Fighter) -> str:
    def d(f):
        return {"name": f.name, "strength": f.strength, "guard": f.guard, "speed": f.speed, "spirit": f.spirit,
                "rank": f.rank, "skills": list(f.skills), "item_bonus": f.item_bonus, "vet_pct": f.vet_pct}
    return json.dumps({"a": d(a), "b": d(b)}, separators=(",", ":"))


def replay_from(fight: Fight) -> Optional[Tuple[cat_battle.Fighter, cat_battle.Fighter, cat_battle.BattleResult]]:
    """Re-run a settled fight from its stored seed and snapshots (any result can be audited)."""
    if fight.snap is None or fight.seed is None:
        return None
    s = json.loads(fight.snap)
    mk = lambda d: cat_battle.Fighter(d["name"], d["strength"], d["guard"], d["speed"], d["spirit"], d["rank"],
                                      tuple(d["skills"]), d["item_bonus"], int(d.get("vet_pct", 0)))
    a, b = mk(s["a"]), mk(s["b"])
    return a, b, cat_battle.fight(a, b, int(fight.seed))


async def accept(fight_id: int, user_id: int, clone_id, cat_id: int, seed: Optional[int] = None) -> AcceptResult:
    """The challenged person accepts with `cat_id`. Both wagers are taken, the fight runs and is settled, or nothing at all
    happens. Statuses that cancel the challenge (the challenger can no longer pay or fight) are saved as 'void'."""
    seed = secrets.randbelow(2 ** 31) if seed is None else int(seed)
    p = await _pool()
    try:
        async with p.acquire() as conn:
            async with conn.transaction():
                return await _accept_tx(conn, int(fight_id), int(user_id), clone_id, int(cat_id), seed)
    except _Abort as e:
        return AcceptResult(e.status, **e.kw)


async def _void(conn, fight_id: int, status: str) -> AcceptResult:
    await conn.execute("UPDATE cat_fights SET status = 'void' WHERE id = $1 AND status = 'pending'", fight_id)
    return AcceptResult(status)


async def _accept_tx(conn, fight_id: int, user_id: int, clone_id, cat_id: int, seed: int) -> AcceptResult:
    row = await conn.fetchrow("SELECT *, (expires_at > NOW()) AS live FROM cat_fights WHERE id = $1 FOR UPDATE", fight_id)
    if row is None:
        return AcceptResult("gone")
    fight = Fight.from_row(row)
    if fight.challenged_id != user_id:
        return AcceptResult("not_yours")
    if fight.status != "pending":
        return AcceptResult("gone")                      # a second tap lands here: already settled
    if not row["live"]:
        await conn.execute("UPDATE cat_fights SET status = 'expired' WHERE id = $1", fight_id)
        return AcceptResult("gone")
    key = fight.clone_key
    if key != _bk(clone_id):
        return AcceptResult("gone")
    me_id, them_id, wager = user_id, fight.challenger_id, fight.wager

    # cats, locked in id order
    cats = {int(r["id"]): r for r in await conn.fetch(
        "SELECT * FROM cat_adoptions WHERE id = ANY($1::bigint[]) ORDER BY id FOR UPDATE", [fight.challenger_cat, cat_id])}
    theirs, mine = cats.get(fight.challenger_cat), cats.get(cat_id)

    async def blocked(cid: int) -> bool:
        return bool(await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open' UNION ALL SELECT 1 FROM market_listings WHERE item_type = 'cat' AND item_ref = $1 AND status = 'open'", cid)
                    or await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", cid)
                    or await conn.fetchval("SELECT 1 FROM cat_fight_requests WHERE cat_id = $1 AND status IN ('open','accepted','locked') "
                                       "AND (status <> 'open' OR expires_at > NOW())", cid)
                    or await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'tired' AND ends_at > NOW()", cid))

    if theirs is None or int(theirs["owner_id"]) != them_id or _bk(theirs["clone_id"]) != key or await blocked(fight.challenger_cat):
        return await _void(conn, fight_id, "cat_them")
    if mine is None or int(mine["owner_id"]) != me_id or _bk(mine["clone_id"]) != key or await blocked(cat_id):
        return AcceptResult("cat_you")                   # the accepter can pick another cat; the challenge stays open

    # both players: Arena on, rating, wager cap
    arena = {int(r["owner_id"]): r for r in await conn.fetch(
        "SELECT owner_id, arena_on, rating, created_at FROM cat_arena WHERE owner_id = ANY($1::bigint[]) AND bot_key = $2 "
        "ORDER BY owner_id FOR UPDATE", [me_id, them_id], key)}
    if not arena.get(me_id) or not arena[me_id]["arena_on"]:
        return AcceptResult("me_off")
    if not arena.get(them_id) or not arena[them_id]["arena_on"]:
        return await _void(conn, fight_id, "them_off")
    if wager_error(wager, arena[me_id]["created_at"]):
        return AcceptResult("new")
    if wager_error(wager, arena[them_id]["created_at"]):
        return await _void(conn, fight_id, "new")

    # daily limits and the pair limit
    day = today()
    for uid in (me_id, them_id):
        await conn.execute("INSERT INTO cat_arena_daily (owner_id, day) VALUES ($1, $2) ON CONFLICT DO NOTHING", uid, day)
    drows = {int(r["owner_id"]): r for r in await conn.fetch(
        "SELECT owner_id, fights, net FROM cat_arena_daily WHERE owner_id = ANY($1::bigint[]) AND day = $2 ORDER BY owner_id FOR UPDATE",
        [me_id, them_id], day)}
    d_me, d_them = drows[me_id], drows[them_id]
    if int(d_me["fights"]) >= FIGHTS_PER_DAY or loss_room(int(d_me["net"])) < wager:
        return AcceptResult("limit_me")
    if int(d_them["fights"]) >= FIGHTS_PER_DAY or loss_room(int(d_them["net"])) < wager:
        return AcceptResult("limit_them")
    earlier = int(await conn.fetchval(
        "SELECT COUNT(*) FROM cat_fights WHERE status = 'settled' AND day = $3 AND "
        "((challenger_id = $1 AND challenged_id = $2) OR (challenger_id = $2 AND challenged_id = $1))", me_id, them_id, day) or 0)
    if earlier >= PAIR_PER_DAY:
        return AcceptResult("pair")

    # escrow: lock both wallets, check both can pay BEFORE anything is taken
    wallets = {int(r["owner_id"]): int(r["coins"]) for r in await conn.fetch(
        "SELECT owner_id, coins FROM cat_wallets WHERE owner_id = ANY($1::bigint[]) AND COALESCE(clone_id, -1) = $2 "
        "ORDER BY owner_id FOR UPDATE", [me_id, them_id], BOT_ALL)}
    if wallets.get(me_id, 0) < wager:
        return AcceptResult("poor", wager=wager)
    if wallets.get(them_id, 0) < wager:
        return await _void(conn, fight_id, "poor_them")
    for uid in (me_id, them_id):
        took = await conn.fetchval("UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                                   "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins", uid, BOT_ALL, wager)
        if took is None:
            raise _Abort("poor", wager=wager)            # can't happen after the check above; rolls back if it ever does

    return await _fight_and_settle(conn, fight, me_id, them_id, cat_id, mine, theirs, arena, day, earlier, key, seed)


async def _fight_and_settle(conn, fight: Fight, me_id: int, them_id: int, cat_id: int, mine, theirs, arena, day, earlier: int,
                            key: int, seed: int) -> AcceptResult:
    """Run the fight and settle it inside the caller's transaction. Both wagers must already be taken. `fight` is a
    pending cat_fights row (a challenge, or one a request accept just inserted); me_id is the person who accepted."""
    fight_id, wager = fight.id, fight.wager
    # fight
    cat_me, cat_them = cat_adopt.Cat.from_row(mine), cat_adopt.Cat.from_row(theirs)
    fx = {}
    for c in (cat_me, cat_them):
        fx[c.id] = bool(await conn.fetchval("SELECT 1 FROM cat_effects WHERE cat_id = $1 AND effect_key = 'hurt' AND ends_at > NOW()", c.id))
    vets = {i: cat_records.bonus_pct(n) for i, n in (await cat_records.fights_in(conn, [cat_them.id, cat_me.id])).items()}
    a = cat_battle.from_cat(cat_them, await cat_stats._load(conn, cat_them.id), hurt=fx[cat_them.id], vet_pct=vets[cat_them.id])
    b = cat_battle.from_cat(cat_me, await cat_stats._load(conn, cat_me.id), hurt=fx[cat_me.id], vet_pct=vets[cat_me.id])
    result = cat_battle.fight(a, b, seed)
    winner_id, loser_id = (them_id, me_id) if result.winner == "a" else (me_id, them_id)
    loser_cat = cat_them.id if result.winner == "b" else cat_me.id
    loser_hp = result.hp[0] if result.winner == "b" else result.hp[1]
    fee, pay = fee_for(wager), payout_for(wager)

    # settle: payout, counters, rating, effects, the fight row
    await conn.execute("UPDATE cat_wallets SET coins = coins + $3, updated_at = NOW() WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                       winner_id, BOT_ALL, pay)
    await conn.execute("UPDATE cat_arena_daily SET fights = fights + 1, net = net + $3 WHERE owner_id = $1 AND day = $2",
                       winner_id, day, pay - wager)
    await conn.execute("UPDATE cat_arena_daily SET fights = fights + 1, net = net - $3 WHERE owner_id = $1 AND day = $2",
                       loser_id, day, wager)
    mis = mismatched(a.power, b.power)
    weaker_is_loser = (a.power < b.power) == (loser_id == them_id) if a.power != b.power else False
    old_w, old_l = int(arena[winner_id]["rating"]), int(arena[loser_id]["rating"])
    gain, loss = rating_change(old_w, old_l, earlier, shield_loser=mis and weaker_is_loser)
    new_w, new_l = old_w + gain, old_l + loss
    await conn.execute("UPDATE cat_arena SET rating = $3, wins = wins + 1, updated_at = NOW() WHERE owner_id = $1 AND bot_key = $2", winner_id, key, new_w)
    await conn.execute("UPDATE cat_arena SET rating = $3, losses = losses + 1, updated_at = NOW() WHERE owner_id = $1 AND bot_key = $2", loser_id, key, new_l)
    win_cat, lose_cat = (cat_them.id, cat_me.id) if result.winner == "a" else (cat_me.id, cat_them.id)
    await cat_records.record_fight(conn, win_cat, lose_cat, result.ended_by == "ko")
    from modules import cat_league
    await cat_league.play_points(conn, key, winner_id, loser_id)      # League points when both are entered (never raises)
    hurt = loser_hp <= 0
    for kind, hours in (("tired", TIRED_HOURS),) + ((("hurt", HURT_HOURS),) if hurt else ()):
        await conn.execute("INSERT INTO cat_effects (cat_id, effect_key, ends_at) VALUES ($1, $2, NOW() + $3 * INTERVAL '1 hour') "
                           "ON CONFLICT (cat_id, effect_key) DO UPDATE SET ends_at = EXCLUDED.ends_at", loser_cat, kind, hours)
    await conn.execute("UPDATE cat_fights SET status = 'settled', challenged_cat = $2, seed = $3, winner_id = $4, ended_by = $5, fee = $6, "
                       "snap = $7, day = $8, settled_at = NOW() WHERE id = $1", fight_id, cat_id, seed, winner_id, result.ended_by, fee, _snap(a, b), day)
    done = Fight(**{**fight.__dict__, "status": "settled", "challenged_cat": cat_id, "seed": seed, "winner_id": winner_id,
                    "ended_by": result.ended_by, "fee": fee})
    return AcceptResult("ok", done, result, a, b, winner_id, loser_id, wager, fee, pay,
                        {winner_id: (new_w, gain), loser_id: (new_l, loss)},
                        hurt, mis)


async def get_ready(cat_id: int) -> Optional[Ready]:
    """One cat with its stats and effects (for a challenge card). None when the cat is gone."""
    p = await _pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM cat_adoptions WHERE id = $1", int(cat_id))
    if row is None:
        return None
    cat = cat_adopt.Cat.from_row(row)
    st = await cat_stats.get_stats(cat.id)
    fx = (await effects_for([cat.id]))[cat.id]
    vet = (await cat_records.bonuses([cat.id])).get(cat.id, 0)
    return Ready(cat, st, fx, cat_battle.from_cat(cat, st, hurt=fx.hurt(), vet_pct=vet))
