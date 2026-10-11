"""Cat trade IDs (7 random words per owner) and Premium cross-server trading."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_adopt as ca
from modules import cat_ids as ids
from modules import cat_setup as cs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def test_generated_id_is_seven_known_words():
    for _ in range(50):
        t = ids.generate()
        parts = t.split("-")
        assert len(parts) == 7 and all(p in ids.WORDS for p in parts)
    assert len({ids.generate() for _ in range(200)}) == 200


def test_word_list_is_clean_and_big_enough():
    assert len(ids.WORDS) >= 300 and len(set(ids.WORDS)) == len(ids.WORDS)
    assert all(re.fullmatch(r"[a-z]+", w) for w in ids.WORDS)


def test_normalize_is_forgiving_but_strict_on_count_and_words():
    t = ids.generate()
    assert ids.normalize(t.upper()) == t
    assert ids.normalize(t.replace("-", " ")) == t
    assert ids.normalize(" " + t.replace("-", ", ") + " ") == t
    assert ids.normalize("-".join(t.split("-")[:6])) is None
    assert ids.normalize(t + "-cat") is None
    assert ids.normalize("zzzz-" + "-".join(t.split("-")[1:])) is None
    assert ids.normalize(None) is None and ids.normalize("") is None


def test_only_premium_trades_across_servers():
    assert ids.can_trade_across_servers(True) is True
    assert ids.can_trade_across_servers(False) is False


def test_migration_and_wiring():
    sql = (ROOT / "database/migrations/037_cat_trade_ids.sql").read_text()
    assert "cat_owner_ids" in sql and "UNIQUE INDEX" in sql and "trade_id" in sql
    db = (ROOT / "database.py").read_text()
    assert "037_cat_trade_ids.sql" in db
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 82


class FakeConn:
    """Stand-in for cat_owner_ids: rows[(owner, clone_key)] = {"short": ..., "words": ...}."""
    def __init__(self):
        self.rows = {}
        self.clash_next = 0                        # make the next N writes hit somebody else's number

    def _owner_of(self, col, value, clone=None):
        for (o, c), r in self.rows.items():
            if r.get(col) == value and (clone is None or c == clone):
                return o
        return None

    async def fetchval(self, sql, *a):
        if "SELECT short_id" in sql:
            return (self.rows.get((a[0], a[1])) or {}).get("short")
        if "SELECT owner_id" in sql:
            col = "short" if "short_id =" in sql else "words"
            return self._owner_of(col, a[0], a[1])

    async def fetchrow(self, sql, *a):
        col = "short" if "short_id =" in sql else "words"
        for (o, c), r in self.rows.items():
            if r.get(col) == a[0]:
                return {"owner_id": o, "clone_id": None if c == -1 else c}
        return None

    async def execute(self, sql, owner, clone_key, *rest):
        if self.clash_next:
            self.clash_next -= 1
            if sql.startswith("UPDATE"):
                raise Exception('duplicate key value violates unique constraint "ux_cat_owner_ids_short"')
            return "INSERT 0 0"
        if sql.startswith("UPDATE"):
            r = self.rows.get((owner, clone_key))
            if r is not None and r.get("short") is None:
                r["short"] = rest[0]
                return "UPDATE 1"
            return "UPDATE 0"
        key = (owner, -1 if clone_key is None else clone_key)
        if key in self.rows or any(r.get("short") == rest[0] for r in self.rows.values()):
            return "INSERT 0 0"
        self.rows[key] = {"short": rest[0], "words": None}
        return "INSERT 0 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *e):
                return False
        return Ctx()


def _patch_pool(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(ids, "_pool", AsyncMock(return_value=FakePool(conn)))
    return conn


def test_id_is_fixed_per_owner_and_separate_per_bot(monkeypatch):
    _patch_pool(monkeypatch)
    a1 = run(ids.get_or_create(1, None))
    assert ids.normalize_short(a1) == a1
    assert run(ids.get_or_create(1, None)) == a1
    assert run(ids.get_or_create(2, None)) != a1
    other_bot = run(ids.get_or_create(1, 7))
    assert run(ids.find_owner(a1, None)) == 1
    assert run(ids.find_owner(a1, 7)) is None             # the main bot's ID doesn't exist on a clone
    assert run(ids.find_owner(other_bot, 7)) == 1
    assert run(ids.find_owner("not an id", None)) is None


def test_safe_get_never_raises(monkeypatch):
    monkeypatch.setattr(ids, "_pool", AsyncMock(side_effect=RuntimeError("db down")))
    assert run(ids.safe_get(1, None)) is None


# ── UI ────────────────────────────────────────────────────────────────────

def _interaction(guild_id=5, user_id=10):
    i = MagicMock()
    i.user = SimpleNamespace(id=user_id, display_name="Me", mention="<@10>")
    i.guild = SimpleNamespace(id=guild_id, name="Cat Club") if guild_id else None
    i.client = SimpleNamespace(clone_id=None, fetch_user=AsyncMock())
    i.response.send_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.channel = SimpleNamespace(send=AsyncMock())
    return i


def _cat(i=1, owner=10):
    return SimpleNamespace(id=i, owner_id=owner, pool_id=1, name="Mochi", rarity="common", trait="sleepy", traded_count=0)


def _parent():
    return SimpleNamespace(current=_cat(), interaction_check=AsyncMock(return_value=True))


def test_free_server_can_open_the_id_box(monkeypatch):
    """Free servers may type an ID (it then only works for people in the same server, see test_cat_free_id_trade)."""
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=False))
    view = v.TradeTargetView(_parent())
    i = _interaction()
    run(view._by_id(i))
    i.response.send_modal.assert_called_once()


def test_premium_server_opens_the_id_modal(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    view = v.TradeTargetView(_parent())
    i = _interaction()
    run(view._by_id(i))
    i.response.send_modal.assert_called_once()


def test_modal_refuses_free_unknown_and_self(monkeypatch):
    modal = v.TradeIdModal(_parent())
    modal.trade_id = SimpleNamespace(__str__=lambda s: "x")
    class T:  # text input stand-in
        def __init__(s, t): s.t = t
        def __str__(s): return s.t
    # free server
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=False))
    monkeypatch.setattr(ids, "find_identity", AsyncMock(return_value=(99, None)))     # exists, but not a member of this server
    modal.trade_id = T("142574D")
    i = _interaction()
    run(modal.on_submit(i))
    assert "Premium" in i.response.send_message.call_args.args[0]
    # premium, unknown id
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    monkeypatch.setattr(ids, "find_identity", AsyncMock(return_value=None))
    i = _interaction()
    run(modal.on_submit(i))
    assert "don't know that ID" in i.response.send_message.call_args.args[0] and ids.lookup_allowed(10)
    # own id
    monkeypatch.setattr(ids, "find_identity", AsyncMock(return_value=(10, None)))
    i = _interaction()
    run(modal.on_submit(i))
    assert "your own" in i.response.send_message.call_args.args[0]


def test_remote_offer_goes_by_dm_not_the_channel(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    v._remote_at.clear(); v._remote_pending.clear()
    target = SimpleNamespace(id=99, display_name="Far", mention="<@99>", send=AsyncMock(return_value=MagicMock()))
    view = v.TradeAskView(_parent(), target, [], remote=True)
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    i = _interaction()
    run(view._chosen(i))
    target.send.assert_called_once()
    i.channel.send.assert_not_called()
    assert v._remote_pending[99] == 1


def test_remote_offer_blocked_when_not_premium_anymore(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=False))
    v._remote_at.clear(); v._remote_pending.clear()
    target = SimpleNamespace(id=99, display_name="Far", mention="<@99>", send=AsyncMock())
    view = v.TradeAskView(_parent(), target, [], remote=True)
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    run(view._chosen(_interaction()))
    target.send.assert_not_called()


def test_remote_offer_cooldown_and_pending_cap(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    v._remote_at.clear(); v._remote_pending.clear()
    target = SimpleNamespace(id=99, display_name="Far", mention="<@99>", send=AsyncMock(return_value=MagicMock()))
    view = v.TradeAskView(_parent(), target, [], remote=True)
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    run(view._chosen(_interaction()))
    i2 = _interaction()
    run(view._chosen(i2))                                   # same sender again right away
    assert "Easy there" in i2.response.send_message.call_args.args[0] and target.send.call_count == 1
    v._remote_at.clear()
    v._remote_pending[99] = v.REMOTE_PENDING_MAX
    i3 = _interaction()
    run(view._chosen(i3))
    assert "already has" in i3.response.send_message.call_args.args[0] and target.send.call_count == 1


def test_dm_closed_is_reported_and_nothing_counted(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    v._remote_at.clear(); v._remote_pending.clear()
    target = SimpleNamespace(id=99, display_name="Far", mention="<@99>",
                             send=AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "closed")))
    view = v.TradeAskView(_parent(), target, [], remote=True)
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    i = _interaction()
    run(view._chosen(i))
    assert "DMs are closed" in i.response.edit_message.call_args.kwargs["content"]
    assert 99 not in v._remote_pending and not v._remote_at


def test_slot_released_and_sender_told_on_answer(monkeypatch):
    v._remote_pending.clear()
    giver = SimpleNamespace(id=10, mention="<@10>", send=AsyncMock())
    taker = SimpleNamespace(id=99, mention="<@99>")
    offer = v.TradeOfferView(SimpleNamespace(clone_id=None), giver, taker, _cat(), None, remote=True)
    offer.counted = True
    v._remote_pending[99] = 1
    inter = MagicMock(); inter.message = None
    inter.response.edit_message = AsyncMock()
    run(offer._finish(inter, "❌ declined"))
    assert 99 not in v._remote_pending
    giver.send.assert_called_once()
    run(offer._finish(inter, "again"))                      # idempotent: never goes negative
    assert 99 not in v._remote_pending


def test_same_server_offer_still_uses_the_channel(monkeypatch):
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=True))
    target = SimpleNamespace(id=99, display_name="Near", mention="<@99>", send=AsyncMock())
    view = v.TradeAskView(_parent(), target, [])
    view.pick = SimpleNamespace(values=[v.GIFT_VALUE])
    i = _interaction()
    i.channel.send = AsyncMock(return_value=MagicMock())
    run(view._chosen(i))
    i.channel.send.assert_called_once()
    target.send.assert_not_called()
