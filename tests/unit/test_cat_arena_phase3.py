"""Cat Arena (fights plan, phase 3): pure rules, the accept transaction against an in-memory fake database,
challenge rules, screen budgets, wiring and help text. Play coins only."""
import asyncio
import copy
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import pytest

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_arena as va
from modules import cat_adopt as ca
from modules import cat_arena as ar
from modules import cat_battle as cb
from modules import cat_help, cat_hub
from modules import cat_stats as cs

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=30)


def run(c):
    return asyncio.run(c)


# ---------- pure rules ----------
def test_wager_ladder_fee_and_payout():
    assert ar.LADDER == (500, 2_500, 10_000, 50_000) and ar.FEE_PERCENT == 8
    assert ar.fee_for(500) == 80 and ar.payout_for(500) == 920         # pot 1,000 minus 8%
    assert all(ar.fee_for(w) + ar.payout_for(w) == 2 * w for w in ar.LADDER)


def test_wager_must_be_on_the_ladder_and_new_players_have_a_cap():
    assert ar.wager_error(500, OLD) is None and ar.wager_error(501, OLD) == "ladder"
    assert ar.wager_error(50_000, NOW - timedelta(hours=1)) == "new" and ar.wager_error(2_500, NOW - timedelta(hours=1)) is None
    assert ar.wager_error(50_000, NOW - timedelta(days=ar.NEW_ARENA_DAYS + 1)) is None


def test_rating_is_zero_sum_shrinks_per_repeat_and_a_shield_costs_nothing():
    w, l = ar.rating_change(1000, 1000)
    assert w == 16 and l == -16
    assert ar.rating_change(1000, 1000, earlier_today=1)[0] == 8 and ar.rating_change(1000, 1000, earlier_today=2)[0] == 4
    assert ar.rating_change(1000, 1000, earlier_today=9)[0] == 4
    w2, l2 = ar.rating_change(1000, 1000, shield_loser=True)
    assert w2 == 16 and l2 == 0
    assert ar.rating_change(1000, ar.RATING_FLOOR)[1] == 0                 # never below the floor
    assert ar.rating_change(900, 1100)[0] > ar.rating_change(1100, 900)[0]  # an upset pays more


def test_matching_band_loss_room_and_effects():
    assert not ar.mismatched(10, 13) and ar.mismatched(10, 14)
    assert ar.loss_room(0) == 100_000 and ar.loss_room(-40_000) == 60_000 and ar.loss_room(5_000) == 100_000
    fx = ar.Effects(NOW + timedelta(hours=1), NOW - timedelta(hours=1))
    assert fx.tired() and not fx.hurt() and "Tired" in ar.effect_text(fx)


def test_odds_for_the_defender_favour_the_stronger_cat():
    strong = cb.Fighter("S", 12, 10, 8, 8, rank=5)
    weak = cb.Fighter("W", 3, 3, 3, 3)
    assert ar.odds_for_defender(weak, strong)[0] in ("Favoured", "Heavy favourite")
    assert ar.odds_for_defender(strong, weak)[0] in ("Long shot", "Even")


# ---------- an in-memory stand-in for the database ----------
class FakeConn:
    """Two players: 1 (the one who accepts) and 2 (the challenger). Cats 11 (player 1) and 10 (player 2)."""

    def __init__(self, coins=(10_000, 10_000)):
        self.cats = {11: self._cat(11, 1, "Mochi"), 10: self._cat(10, 2, "Biscuit")}
        self.cat_stats = {11: dict(strength=5, guard=3, speed=3, spirit=2), 10: dict(strength=5, guard=3, speed=3, spirit=2)}
        self.wallets = {1: coins[0], 2: coins[1]}
        self.arena = {u: {"owner_id": u, "arena_on": True, "rating": 1000, "created_at": OLD, "wins": 0, "losses": 0} for u in (1, 2)}
        self.daily = {}
        self.effects = {}                       # (cat, key) -> hours
        self.listed, self.out = set(), set()
        self.fights = {1: dict(id=1, mode="local", clone_key=-1, guild_id=5, challenger_id=2, challenged_id=1, challenger_cat=10,
                               challenged_cat=None, wager=500, fee=0, status="pending", seed=None, winner_id=None, ended_by=None,
                               snap=None, day=None, expires_at=NOW + timedelta(minutes=9), live=True, settled_at=None)}
        self.next_fight = 2
        self.fail_at = None

    @staticmethod
    def _cat(cid, owner, name):
        return dict(id=cid, owner_id=owner, clone_id=None, pool_id=1, name=name, rarity="common", trait="Brave", adopted_at=OLD,
                    traded_count=0, cat_rank=0)

    @asynccontextmanager
    async def transaction(self):
        snap = copy.deepcopy((self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats))
        try:
            yield
        except BaseException:
            self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats = snap
            raise

    async def fetchrow(self, sql, *a):
        if "FROM cat_fights WHERE id" in sql:
            f = self.fights.get(a[0])
            return dict(f) if f else None
        if "FROM cat_adoptions WHERE id" in sql:
            return self.cats.get(a[0])
        if "FROM cat_stats" in sql:
            return {**self.cat_stats[a[0]], "respec_count": 0}
        if "FROM cat_arena_daily" in sql:
            d = self.daily.get((a[0], a[1]))
            return dict(d) if d else None
        if "FROM cat_arena " in sql:
            return self.arena.get(a[0])

    async def fetch(self, sql, *a):
        if "FROM cat_adoptions WHERE id = ANY" in sql:
            return [self.cats[i] for i in sorted(set(a[0])) if i in self.cats]
        if "FROM cat_skills" in sql:
            return []
        if "FROM cat_arena WHERE owner_id = ANY" in sql:
            return [self.arena[u] for u in sorted(a[0]) if u in self.arena]
        if "FROM cat_arena_daily" in sql:
            return [{"owner_id": u, **self.daily[(u, a[1])]} for u in sorted(a[0]) if (u, a[1]) in self.daily]
        if "FROM cat_wallets" in sql:
            return [{"owner_id": u, "coins": self.wallets[u]} for u in sorted(a[0]) if u in self.wallets]
        return []

    async def fetchval(self, sql, *a):
        if "FROM cat_listings" in sql:
            return 1 if a[0] in self.listed else None
        if "FROM cat_outings" in sql:
            return 1 if a[0] in self.out else None
        if "FROM cat_effects" in sql:
            key = "tired" if "'tired'" in sql else "hurt"
            return 1 if (a[0], key) in self.effects else None
        if "SELECT COUNT(*) FROM cat_fights WHERE status = 'settled'" in sql:
            x, y, day = a
            return sum(1 for f in self.fights.values() if f["status"] == "settled" and f["day"] == day
                       and {f["challenger_id"], f["challenged_id"]} == {x, y})
        if "UPDATE cat_wallets SET coins = coins - $3" in sql:
            if self.fail_at == "take" and a[0] == 2:
                raise RuntimeError("boom")
            if self.wallets.get(a[0], 0) >= a[2]:
                self.wallets[a[0]] -= a[2]
                return self.wallets[a[0]]
            return None
        if "SELECT coins FROM cat_wallets" in sql:
            return self.wallets.get(a[0])
        if "SELECT fights, net FROM cat_arena_daily" in sql:
            return None
        if "SELECT COUNT(*) FROM cat_fights WHERE challenger_id" in sql:
            return sum(1 for f in self.fights.values() if f["challenger_id"] == a[0] and f["status"] == "pending")
        if "SELECT 1 FROM cat_fights WHERE challenger_id" in sql:
            return 1 if any(f["challenger_id"] == a[0] and f["challenged_id"] == a[1] and f["status"] == "pending" for f in self.fights.values()) else None
        if sql.startswith("INSERT INTO cat_fights"):
            fid = self.next_fight
            self.next_fight += 1
            self.fights[fid] = dict(id=fid, mode="local", clone_key=a[0], guild_id=a[1], challenger_id=a[2], challenged_id=a[3],
                                    challenger_cat=a[4], challenged_cat=None, wager=a[5], fee=0, status="pending", seed=None,
                                    winner_id=None, ended_by=None, snap=None, day=None, expires_at=NOW + timedelta(minutes=a[6]),
                                    live=True, settled_at=None)
            return fid

    async def execute(self, sql, *a):
        if sql.startswith("UPDATE cat_fights SET status = 'void'"):
            if self.fights[a[0]]["status"] == "pending":
                self.fights[a[0]]["status"] = "void"
        elif sql.startswith("UPDATE cat_fights SET status = 'expired' WHERE id"):
            self.fights[a[0]]["status"] = "expired"
        elif sql.startswith("UPDATE cat_fights SET status = 'expired' WHERE status"):
            pass
        elif sql.startswith("INSERT INTO cat_arena_daily"):
            self.daily.setdefault((a[0], a[1]), {"fights": 0, "net": 0})
        elif sql.startswith("UPDATE cat_wallets SET coins = coins + $3"):
            self.wallets[a[0]] += a[2]
        elif sql.startswith("UPDATE cat_arena_daily SET fights = fights + 1, net = net + $3"):
            self.daily[(a[0], a[1])]["fights"] += 1
            self.daily[(a[0], a[1])]["net"] += a[2]
        elif sql.startswith("UPDATE cat_arena_daily SET fights = fights + 1, net = net - $3"):
            self.daily[(a[0], a[1])]["fights"] += 1
            self.daily[(a[0], a[1])]["net"] -= a[2]
        elif "SET rating = $3, wins = wins + 1" in sql:
            self.arena[a[0]].update(rating=a[2], wins=self.arena[a[0]]["wins"] + 1)
        elif "SET rating = $3, losses = losses + 1" in sql:
            self.arena[a[0]].update(rating=a[2], losses=self.arena[a[0]]["losses"] + 1)
        elif sql.startswith("INSERT INTO cat_effects"):
            self.effects[(a[0], a[1])] = a[2]
        elif sql.startswith("UPDATE cat_fights SET status = 'settled'"):
            f = self.fights[a[0]]
            f.update(status="settled", challenged_cat=a[1], seed=a[2], winner_id=a[3], ended_by=a[4], fee=a[5], snap=a[6], day=a[7])


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = FakeConn(**kw)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        async def get_pool():
            return Pool()
        monkeypatch.setattr(ar, "_pool", get_pool)
        return conn
    return make


def total(conn):
    return sum(conn.wallets.values())


# ---------- accept: escrow, fight and settle in one transaction ----------
def test_accept_settles_pays_the_winner_and_the_fee_leaves_the_game(db):
    conn = db()
    before = total(conn)
    res = run(ar.accept(1, 1, None, 11, seed=7))
    assert res.status == "ok" and res.winner_id in (1, 2) and res.loser_id in (1, 2) and res.winner_id != res.loser_id
    assert res.wager == 500 and res.fee == 80 and res.payout == 920
    assert conn.wallets[res.winner_id] == 10_000 + 500 - 80 and conn.wallets[res.loser_id] == 10_000 - 500
    assert total(conn) == before - 80                                 # the 8% fee is a coin sink
    f = conn.fights[1]
    assert f["status"] == "settled" and f["seed"] == 7 and f["winner_id"] == res.winner_id and f["challenged_cat"] == 11 and f["fee"] == 80
    assert conn.daily[(1, f["day"])]["fights"] == 1 and conn.daily[(2, f["day"])]["fights"] == 1
    assert conn.daily[(res.winner_id, f["day"])]["net"] == 420 and conn.daily[(res.loser_id, f["day"])]["net"] == -500


def test_the_loser_keeps_their_cat_and_it_gets_tired_and_hurt_only_after_a_knock_out(db):
    conn = db()
    res = run(ar.accept(1, 1, None, 11, seed=3))
    loser_cat = 10 if res.loser_id == 2 else 11
    assert conn.cats[10]["owner_id"] == 2 and conn.cats[11]["owner_id"] == 1               # nobody loses a cat
    assert (loser_cat, "tired") in conn.effects and conn.effects[(loser_cat, "tired")] == ar.TIRED_HOURS
    assert ((loser_cat, "hurt") in conn.effects) == res.hurt
    winner_cat = 11 if loser_cat == 10 else 10
    assert (winner_cat, "tired") not in conn.effects and (winner_cat, "hurt") not in conn.effects


def test_ratings_and_win_counts_move_together(db):
    conn = db()
    res = run(ar.accept(1, 1, None, 11, seed=5))
    w, l = conn.arena[res.winner_id], conn.arena[res.loser_id]
    assert w["wins"] == 1 and l["losses"] == 1 and w["rating"] > 1000 and l["rating"] < 1000
    assert (w["rating"] - 1000) == -(l["rating"] - 1000)
    assert res.rating[res.winner_id] == (w["rating"], w["rating"] - 1000)


def test_a_settled_fight_replays_from_its_seed_and_snapshots(db):
    conn = db()
    res = run(ar.accept(1, 1, None, 11, seed=11))
    fight = ar.Fight.from_row(conn.fights[1])
    a, b, again = ar.replay_from(fight)
    assert again.log() == res.result.log() and (again.winner == "a") == (res.winner_id == 2)
    assert json.loads(fight.snap)["a"]["name"] == "Biscuit"


def test_two_taps_on_accept_only_settle_once(db):
    conn = db()
    first = run(ar.accept(1, 1, None, 11, seed=1))
    after_first = dict(conn.wallets)
    second = run(ar.accept(1, 1, None, 11, seed=2))
    assert first.status == "ok" and second.status == "gone"
    assert conn.wallets == after_first and conn.fights[1]["seed"] == 1
    assert sum(d["fights"] for d in conn.daily.values()) == 2          # counted once per player


def test_a_crash_inside_the_transaction_takes_nothing(db):
    conn = db()
    conn.fail_at = "take"                                             # the second wager fails after the first was taken
    with pytest.raises(RuntimeError):
        run(ar.accept(1, 1, None, 11, seed=1))
    assert conn.wallets == {1: 10_000, 2: 10_000} and conn.fights[1]["status"] == "pending" and not conn.effects


def test_accepter_without_coins_changes_nothing_and_the_challenge_stays_open(db):
    conn = db(coins=(100, 10_000))
    res = run(ar.accept(1, 1, None, 11))
    assert res.status == "poor" and conn.wallets == {1: 100, 2: 10_000} and conn.fights[1]["status"] == "pending"


def test_challenger_without_coins_voids_the_challenge(db):
    conn = db(coins=(10_000, 100))
    res = run(ar.accept(1, 1, None, 11))
    assert res.status == "poor_them" and conn.wallets == {1: 10_000, 2: 100} and conn.fights[1]["status"] == "void"


def test_only_the_challenged_player_can_accept_and_expired_challenges_are_gone(db):
    conn = db()
    assert run(ar.accept(1, 2, None, 10)).status == "not_yours" and run(ar.accept(1, 3, None, 11)).status == "not_yours"
    conn.fights[1]["live"] = False
    assert run(ar.accept(1, 1, None, 11)).status == "gone" and conn.fights[1]["status"] == "expired"
    assert conn.wallets == {1: 10_000, 2: 10_000}
    assert run(ar.accept(99, 1, None, 11)).status == "gone"


def test_cats_that_cannot_fight(db):
    conn = db()
    conn.effects[(11, "tired")] = 2
    assert run(ar.accept(1, 1, None, 11)).status == "cat_you" and conn.fights[1]["status"] == "pending"
    conn.effects.clear()
    conn.listed.add(11)
    assert run(ar.accept(1, 1, None, 11)).status == "cat_you"
    conn.listed.clear()
    conn.out.add(11)
    assert run(ar.accept(1, 1, None, 11)).status == "cat_you"
    conn.out.clear()
    assert run(ar.accept(1, 1, None, 10)).status == "cat_you"                         # not their cat
    conn.listed.add(10)                                                                # the challenger's cat went on sale
    assert run(ar.accept(1, 1, None, 11)).status == "cat_them" and conn.fights[1]["status"] == "void"
    assert conn.wallets == {1: 10_000, 2: 10_000}


def test_both_players_need_arena_on(db):
    conn = db()
    conn.arena[1]["arena_on"] = False
    assert run(ar.accept(1, 1, None, 11)).status == "me_off" and conn.fights[1]["status"] == "pending"
    conn.arena[1]["arena_on"], conn.arena[2]["arena_on"] = True, False
    assert run(ar.accept(1, 1, None, 11)).status == "void" or conn.fights[1]["status"] == "void"
    assert conn.wallets == {1: 10_000, 2: 10_000}


def test_daily_fight_loss_and_pair_limits(db):
    conn = db()
    day = ar.today()
    conn.daily[(1, day)] = {"fights": ar.FIGHTS_PER_DAY, "net": 0}
    assert run(ar.accept(1, 1, None, 11)).status == "limit_me"
    conn.daily[(1, day)] = {"fights": 0, "net": -(ar.DAILY_NET_LOSS_LIMIT - 400)}              # only 400 coins of room, wager is 500
    assert run(ar.accept(1, 1, None, 11)).status == "limit_me"
    conn.daily[(1, day)] = {"fights": 0, "net": 0}
    conn.daily[(2, day)] = {"fights": ar.FIGHTS_PER_DAY, "net": 0}
    assert run(ar.accept(1, 1, None, 11)).status == "limit_them"
    conn.daily[(2, day)] = {"fights": 0, "net": 0}
    for i in range(10, 13):
        conn.fights[100 + i] = {**conn.fights[1], "id": 100 + i, "status": "settled", "day": day}
    assert run(ar.accept(1, 1, None, 11)).status == "pair"
    assert conn.wallets == {1: 10_000, 2: 10_000} and conn.fights[1]["status"] == "pending"


def test_new_arena_players_cannot_take_a_big_wager(db):
    conn = db()
    conn.fights[1]["wager"] = 50_000
    conn.wallets = {1: 100_000, 2: 100_000}
    conn.arena[1]["created_at"] = NOW
    assert run(ar.accept(1, 1, None, 11)).status == "new" and conn.wallets == {1: 100_000, 2: 100_000}


def test_a_hurt_cat_fights_at_minus_ten_percent():
    cat = ca.Cat(id=1, owner_id=1, clone_id=None, pool_id=1, name="X", rarity="common", trait="Brave")
    st = cs.Stats(strength=4, guard=4)
    assert cb.from_cat(cat, st, hurt=True).strength < cb.from_cat(cat, st, hurt=False).strength


# ---------- sending a challenge ----------
def test_send_challenge_rules(db):
    conn = db()
    conn.fights.clear()
    send = lambda **kw: run(ar.send_challenge(*(kw.get(k, d) for k, d in (("me", 2), ("clone", None), ("them", 1), ("cat", 10), ("wager", 500), ("guild", 5)))))
    ok = send()
    assert ok.status == "ok" and conn.fights[ok.fight_id]["status"] == "pending" and conn.wallets == {1: 10_000, 2: 10_000}   # no coins move yet
    assert send().status == "pending"                                              # one open challenge per pair
    assert send(me=1, them=1).status == "self" and send(wager=777).status == "ladder"
    conn.fights.clear()
    assert send(cat=11).status == "cat" and send(wager=50_000).status == "poor"
    conn.effects[(10, "tired")] = 1
    assert send().status == "cat"
    conn.effects.clear()
    conn.arena[1]["arena_on"] = False
    assert send().status == "them_off"
    conn.arena[1]["arena_on"], conn.arena[2]["arena_on"] = True, False
    assert send().status == "me_off"


def test_a_player_can_only_have_three_challenges_out(db):
    conn = db()
    conn.fights.clear()
    for who in (3, 4, 5):
        conn.arena[who] = {"owner_id": who, "arena_on": True, "rating": 1000, "created_at": OLD}
    out = [run(ar.send_challenge(2, None, who, 10, 500)).status for who in (1, 3, 4, 5)]
    assert out == ["ok", "ok", "ok", "pending"]


# ---------- screens: 40 components, 4000 characters ----------
def _count(view):
    return sum(1 for _ in view.walk_children())


def ready(name="Mochi", cid=11, rank=3):
    cat = ca.Cat(id=cid, owner_id=1, clone_id=None, pool_id=1, name=name, rarity="legendary", trait="Brave", cat_rank=rank)
    st = cs.Stats(strength=20, guard=15, speed=15, spirit=14, skills=("pounce", "hiss", "fluff_guard"))
    fx = ar.Effects(NOW + timedelta(hours=1), NOW + timedelta(hours=3))
    return ar.Ready(cat, st, fx, cb.from_cat(cat, st))


def fight(wager=50_000):
    return ar.Fight(id=1, challenger_id=2, challenged_id=1, challenger_cat=10, wager=wager, status="pending", expires_at=NOW + timedelta(minutes=9))


def test_arena_screen_fits():
    st = ar.ArenaState(True, 1234, 20, 11, 9, -45_000, OLD)
    recent = [f"🏆 Won against <@{123456789012345678}> · +46,000 🪙"] * 3
    view = va.ArenaView()
    view.st, view.incoming, view.recent, view.out_n = st, [fight()] * 5, recent, 3
    view._build()
    assert _count(view) <= 40 and len(va.arena_text(st, 5, 3, recent, "x" * 100)) < 4000
    vis.attach_back(view)
    assert _count(view) <= 40


def test_challenge_and_incoming_screens_fit():
    cats = [ready(f"Cat{i}", i) for i in range(12)]
    cv = va.ChallengeView(object(), cats, "note")
    cv.target = 99
    cv._build()
    assert _count(cv) <= 40 and len(cv.text()) < 4000
    vis.attach_back(cv)
    assert _count(cv) <= 40
    iv = va.IncomingView(object(), [fight()] * 3, 0, ready("Biscuit", 10), cats, cats[0], ("Even", "⚖️"), True, "note")
    assert _count(iv) <= 40
    assert len(va.challenge_text(fight(), ready("B", 10), cats[0], ("Even", "⚖️"), True, "n")) < 4000
    assert "no cat that is ready" in va.challenge_text(fight(), ready("B", 10), None)


def test_replay_and_result_screens_fit(db):
    conn = db()
    res = run(ar.accept(1, 1, None, 11, seed=9))
    for n in range(1, len(res.result.rounds) + 1):
        assert len(va.replay_text(res.a, res.b, res.result, n, res.wager)) < 4000
    for uid in (1, 2):
        txt = va.result_text(res, uid)
        assert len(txt) < 4000 and "Fight code" in txt and ("wins" in txt)
    assert _count(va._screen(va.result_text(res, 1), [discord.ui.ActionRow(va._btn("Back", None))])) <= 40


# ---------- wiring and rules text ----------
def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/058_cat_arena.sql").read_text()
    for t in ("cat_arena", "cat_arena_daily", "cat_fights", "cat_effects"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in sql
    assert not re.search(r"\bDROP\b|(?<!ON )\bDELETE\b|\bTRUNCATE\b|ALTER TABLE", sql, re.I)
    db_src = (ROOT / "database.py").read_text()
    assert "058_cat_arena.sql" in db_src and "adds 058_cat_arena.sql" in db_src


def test_arena_is_in_the_menu_and_the_buttons_persist():
    assert "arena" in [k for k, *_ in cat_hub.MENU] and len(cat_hub.MENU) <= 25
    from discord_bot.cogs import _views_cat_adopt as adopt
    from discord_bot.cogs import _views_cat_hub as hub
    assert isinstance(hub._button_for("arena"), va.ArenaButton)
    assert va.AcceptButton in adopt.DYNAMIC_ITEMS and va.DeclineButton in adopt.DYNAMIC_ITEMS
    assert re.match(va.AcceptButton.__discord_ui_compiled_template__.pattern, "cat_fight:accept:42")
    assert re.match(va.DeclineButton.__discord_ui_compiled_template__.pattern, "cat_fight:decline:7")


def test_no_new_slash_commands():
    src = (ROOT / "discord_bot/cogs/_views_cat_arena.py").read_text() + (ROOT / "modules/cat_arena.py").read_text()
    assert "app_commands" not in src and "@bot.tree" not in src


def test_help_matches_the_game_and_the_old_promise_is_gone():
    text = cat_help.build_text()
    short = cat_help.topic_text("arena")
    for must in (f"{ar.FEE_PERCENT}%", f"{ar.FIGHTS_PER_DAY} fights a day", f"{ar.DAILY_NET_LOSS_LIMIT:,}", f"{ar.EXPIRE_MINUTES} minutes",
                 f"{ar.TIRED_HOURS}h", f"{ar.HURT_HOURS}h", "Accept"):
        assert must in text, must
    assert "500" in short and "50,000" in short and f"{ar.FEE_PERCENT}%" in short and "Accept" in short
    assert "Never hurt, never lost" not in text and "pretend places" in text
    assert "Arena" in [t[2] for t in cat_help.topics()]
    life = (ROOT / "discord_bot/cogs/_views_cat_life.py").read_text()
    assert "never hurt or lost" not in life
    assert f"{', '.join(f'{w:,}' for w in ar.LADDER)}" in va.rules_line()


def test_practice_and_heists_are_unchanged():
    from modules import cat_practice as cp
    assert cp.DAILY_REWARDED_WINS == 10 and cp.COOLDOWN_SECONDS == 45
    from modules import cat_life
    assert cat_life.DAILY_LOSS_LIMIT == 20_000
