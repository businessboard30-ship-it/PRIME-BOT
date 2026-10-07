"""XP slice two: buddy XP from the daily reward, and the Evolve button on the level-up message."""
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import discord_bot.cogs._views_catch_items as items_views
import discord_bot.cogs._views_catch_levelup as lv
from discord_bot.cogs import catch as cog
from discord_bot.cogs._views_catch_creature import EvolveConfirmView
from modules import catch_xp
from modules.catch_coin_card import DAILY_FILE
from modules.catch_i18n import text
from modules.catch_items import DailyResult, DailyReward
from modules.catch_profile import ProfileCreature
from modules.catch_xp import XpResult
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow
from tests.unit.test_catch_phase3 import detail, ready_preview
from tests.unit.notice_helpers import body, shown


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the XP amount

def test_daily_amount_grows_with_the_streak_and_stops_at_a_week():
    assert catch_xp.daily_xp_amount(0) == catch_xp.DAILY_XP_BASE
    assert catch_xp.daily_xp_amount(1) == catch_xp.DAILY_XP_BASE + catch_xp.DAILY_XP_PER_STREAK_DAY
    top = catch_xp.DAILY_XP_BASE + catch_xp.DAILY_XP_PER_STREAK_DAY * catch_xp.DAILY_XP_STREAK_CAP
    assert catch_xp.daily_xp_amount(7) == top == catch_xp.daily_xp_amount(8) == catch_xp.daily_xp_amount(365)
    assert catch_xp.daily_xp_amount(-4) == catch_xp.DAILY_XP_BASE


def test_daily_amount_fits_the_per_grant_limit_of_the_daily_source():
    assert catch_xp.daily_xp_amount(10**6) <= catch_xp.SOURCES["daily"][0]


# ---------------------------------------------------------------- the grant wrapper

def _fake_db(monkeypatch, buddy_id):
    asked = []

    class Db:
        async def fetchval(self, *args):
            asked.append(args)
            return buddy_id

    @contextlib.asynccontextmanager
    async def connection(conn=None):
        yield Db()

    monkeypatch.setattr(catch_xp.catch_db, "connection", connection)
    return asked


def _spy_grant(monkeypatch, result=None):
    calls = []

    async def grant(owned_id, user_id, clone_id, **kw):
        calls.append((owned_id, user_id, clone_id, kw))
        return result

    monkeypatch.setattr(catch_xp, "grant_xp", grant)
    return calls


def test_no_buddy_means_no_grant(monkeypatch):
    _fake_db(monkeypatch, None)
    calls = _spy_grant(monkeypatch)
    assert run(catch_xp.grant_buddy_daily_xp(7, None, streak=3, claim_ref="1")) is None
    assert calls == []


def test_buddy_gets_a_daily_grant_keyed_by_the_claim(monkeypatch):
    _fake_db(monkeypatch, 42)
    won = XpResult(True, None, 35, 4, 4, 9)
    calls = _spy_grant(monkeypatch, won)
    got = run(catch_xp.grant_buddy_daily_xp(7, None, streak=3, claim_ref="1700000000", guild_id=5))
    assert got is won
    owned_id, user_id, clone_id, kw = calls[0]
    assert (owned_id, user_id, clone_id) == (42, 7, None)
    assert kw["source"] == "daily" and kw["amount"] == catch_xp.daily_xp_amount(3)
    assert kw["idem_key"] == "daily-claim:1700000000" and kw["guild_id"] == 5


def test_two_claims_never_share_a_key(monkeypatch):
    _fake_db(monkeypatch, 42)
    calls = _spy_grant(monkeypatch)
    run(catch_xp.grant_buddy_daily_xp(7, None, streak=1, claim_ref="100"))
    run(catch_xp.grant_buddy_daily_xp(7, None, streak=2, claim_ref="200"))
    assert calls[0][3]["idem_key"] != calls[1][3]["idem_key"]


# ---------------------------------------------------------------- the daily screen

SOON = datetime.now(timezone.utc) + timedelta(hours=20)
CLAIMED = DailyResult(True, 3, SOON, DailyReward(250, {}))


def _daily(monkeypatch, result, grant):
    rec = Recorder()
    monkeypatch.setattr(items_views, "check_player_allowed",
                        slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(items_views, "ensure_starter_kit", slow(rec, "starter", False))
    monkeypatch.setattr(items_views, "claim_daily", slow(rec, "claim", result))
    calls, sent, order = [], [], []

    async def fake_grant(user_id, clone_id, **kw):
        calls.append((user_id, clone_id, kw))
        if isinstance(grant, Exception):
            raise grant
        return grant

    async def send(*args, **kwargs):
        order.append("reward")
        sent.append(kwargs)

    async def fake_levelup(interaction, xp, **kw):
        order.append("levelup")
        sent.append({"levelup": xp})

    monkeypatch.setattr(items_views, "grant_buddy_daily_xp", fake_grant)
    monkeypatch.setattr(items_views, "send_levelup", fake_levelup)
    inter = make_interaction(rec)
    inter.followup = SimpleNamespace(send=send)
    return inter, calls, sent, order


def test_claimed_daily_adds_the_buddy_xp_field_and_keeps_the_reward_card(monkeypatch):
    xp = XpResult(True, None, 35, 4, 4, 9)
    inter, calls, sent, _ = _daily(monkeypatch, CLAIMED, xp)
    run(items_views.open_daily(inter))
    embed = sent[0]["embed"]
    field = [f for f in embed.fields if f.name == text("xp.field")]
    assert len(field) == 1 and field[0].value == text("xp.daily_gain", gained=35, level=4)
    assert sent[0]["file"].filename == DAILY_FILE  # reward card still attached
    user_id, clone_id, kw = calls[0]
    assert user_id == 7 and kw["streak"] == 3 and kw["claim_ref"] == str(int(SOON.timestamp()))


def test_a_daily_level_up_uses_the_level_up_wording_and_sends_the_card_after_the_reward(monkeypatch):
    xp = XpResult(True, None, 35, 4, 5, 2)
    inter, _, sent, order = _daily(monkeypatch, CLAIMED, xp)
    run(items_views.open_daily(inter))
    field = [f for f in sent[0]["embed"].fields if f.name == text("xp.field")][0]
    assert field.value == text("xp.daily_level_up", gained=35, level=5)
    assert order == ["reward", "levelup"] and sent[1]["levelup"] is xp


@pytest.mark.parametrize("grant", [
    None, XpResult(False, "capped"), XpResult(True, None, 0, 4, 4, 9), RuntimeError("db down"),
])
def test_no_xp_field_when_nothing_was_gained_and_a_failure_never_hides_the_reward(monkeypatch, grant):
    inter, _, sent, _ = _daily(monkeypatch, CLAIMED, grant)
    run(items_views.open_daily(inter))
    assert text("xp.field") not in [f.name for f in sent[0]["embed"].fields]
    assert "file" in sent[0] and sent[0]["embed"].title


def test_not_ready_daily_never_grants_xp(monkeypatch):
    inter, calls, sent, _ = _daily(monkeypatch, DailyResult(False, 2, SOON), XpResult(True, None, 35, 4, 5, 2))
    run(items_views.open_daily(inter))
    assert calls == [] and text("xp.field") not in [f.name for f in sent[0]["embed"].fields]


# ---------------------------------------------------------------- the level-up message

class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs)


def _interaction():
    return SimpleNamespace(user=SimpleNamespace(id=77), client=SimpleNamespace(clone_id=None),
                           guild_id=None, followup=Followup())


def _buddy(level, species_id=1, name="Cindrop"):
    return ProfileCreature(9, name, "common", level, None, False, False, "ember", None, species_id)


LEVELED = XpResult(True, None, 25, 15, 16, 3)


def _daily_levelup(monkeypatch, xp, buddy):
    async def fake_profile(user_id, clone_id, **kw):
        if isinstance(buddy, Exception):
            raise buddy
        return SimpleNamespace(buddy=buddy)

    monkeypatch.setattr(lv, "load_profile", fake_profile)
    it = _interaction()
    run(lv.send_levelup(it, xp))
    return it.followup.sent


def _cog_levelup(monkeypatch, xp, buddy):
    async def fake_profile(user_id, clone_id, **kw):
        return SimpleNamespace(buddy=buddy)

    monkeypatch.setattr(cog, "load_profile", fake_profile)
    it = _interaction()
    run(cog.SpawnClaimView._send_levelup(object(), it, xp))
    return it.followup.sent


@pytest.mark.parametrize("sender", [_daily_levelup, _cog_levelup])
def test_an_evolve_ready_buddy_gets_the_button_on_both_paths(monkeypatch, sender):
    sent = sender(monkeypatch, LEVELED, _buddy(16))
    assert len(sent) == 1
    view = sent[0]["view"]
    assert isinstance(view, lv.LevelUpEvolveView)
    assert (view.user_id, view.clone_id, view.owned_id) == (77, None, 9)
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert [b.label for b in buttons] == [text("creature.btn_evolve")]


@pytest.mark.parametrize("sender", [_daily_levelup, _cog_levelup])
@pytest.mark.parametrize("buddy", [
    _buddy(10),                                   # evolves at 16, not there yet
    _buddy(20, 16, "Cragtitan"),                  # final form
])
def test_no_button_when_there_is_nothing_to_evolve(monkeypatch, sender, buddy):
    leveled = XpResult(True, None, 25, buddy.level - 1, buddy.level, 3)
    sent = sender(monkeypatch, leveled, buddy)
    assert len(sent) == 1 and "view" not in sent[0]


def test_the_daily_level_up_message_uses_the_daily_wording(monkeypatch):
    sent = _daily_levelup(monkeypatch, LEVELED, _buddy(16))
    assert sent[0]["embed"].description == text("xp.daily_level_up", gained=25, level=16)
    assert sent[0]["ephemeral"] is True and sent[0]["file"].filename


@pytest.mark.parametrize("xp,buddy", [
    (None, _buddy(16)),
    (XpResult(True, None, 10, 15, 15, 10), _buddy(15)),   # no level gained
    (LEVELED, _buddy(12)),                                # buddy changed since
    (LEVELED, None),                                      # no buddy any more
    (LEVELED, RuntimeError("db down")),                   # profile load fails
])
def test_daily_level_up_sends_nothing_when_it_should_not_and_never_raises(monkeypatch, xp, buddy):
    assert _daily_levelup(monkeypatch, xp, buddy) == []


def test_a_card_failure_sends_nothing_and_does_not_raise(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(lv, "levelup_card_png", boom)
    assert _daily_levelup(monkeypatch, LEVELED, _buddy(16)) == []


def test_a_button_failure_still_sends_the_card_without_it(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("view broke")

    monkeypatch.setattr(lv, "LevelUpEvolveView", boom)
    sent = _daily_levelup(monkeypatch, LEVELED, _buddy(16))
    assert len(sent) == 1 and "view" not in sent[0]


def test_evolve_view_for_handles_missing_pieces():
    species = {"id": 1, "evolves_to": 2, "evolve_level": 16}
    buddy = SimpleNamespace(id=9)
    assert lv.evolve_view_for(None, buddy, level=16, user_id=7, clone_id=None) is None
    assert lv.evolve_view_for(species, None, level=16, user_id=7, clone_id=None) is None
    assert lv.evolve_view_for(species, buddy, level=15, user_id=7, clone_id=None) is None
    assert lv.evolve_view_for(species, SimpleNamespace(), level=16, user_id=7, clone_id=None) is None
    assert isinstance(lv.evolve_view_for(species, buddy, level=16, user_id=7, clone_id=None), lv.LevelUpEvolveView)


# ---------------------------------------------------------------- the button

def _click(monkeypatch, *, gate=True, creature="ok", pv="ok", user_id=7, boom=False):
    rec = Recorder()
    monkeypatch.setattr(lv, "check_player_allowed", slow(
        rec, "gate", SimpleNamespace(allowed=gate, reason=None if gate else "disabled")))
    d = detail(id=9) if creature == "ok" else None
    p = ready_preview(owned_id=9) if pv == "ok" else (None if pv is None else pv)

    async def load(owned_id, uid, clone_id):
        rec.add("load")
        if boom:
            raise RuntimeError("db down")
        return d

    async def prev(owned_id, uid, clone_id):
        rec.add("preview")
        return p

    monkeypatch.setattr(lv, "load_creature", load)
    monkeypatch.setattr(lv, "preview", prev)
    inter = make_interaction(rec)
    inter.user = SimpleNamespace(id=user_id)
    edits, followups = [], []

    async def edit(*args, **kwargs):
        rec.add("edit_original")
        edits.append(kwargs)

    async def follow(*args, **kwargs):
        rec.add("followup")
        followups.append((args, kwargs))

    inter.edit_original_response = edit
    inter.followup = SimpleNamespace(send=follow)
    view = lv.LevelUpEvolveView(7, None, 9)
    ok = run(view.interaction_check(inter))
    if ok:
        run(view._evolve(inter))
    return rec, edits, followups, view, ok


def test_the_button_defers_first_then_opens_the_evolve_confirm_card(monkeypatch):
    rec, edits, followups, view, _ = _click(monkeypatch)
    assert rec.calls[0] == "response", rec.calls
    assert rec.calls.index("gate") < rec.calls.index("load") and rec.calls[-1] == "edit_original"
    assert followups == [] and len(edits) == 1
    shown = edits[0]
    assert isinstance(shown["view"], EvolveConfirmView) and shown["content"] is None
    assert [a.filename for a in shown["attachments"]] == ["evolve.png"]
    assert shown["view"].creature_view.detail.id == 9
    assert shown["view"].creature_view.back == view._closed
    assert not view._busy


def test_only_the_owner_can_press_it(monkeypatch):
    rec, edits, followups, _, ok = _click(monkeypatch, user_id=8)
    assert ok is False and edits == [] and "gate" not in rec.calls and "load" not in rec.calls


def test_a_refused_gate_tells_the_player_and_changes_nothing(monkeypatch):
    rec, edits, followups, view, _ = _click(monkeypatch, gate=False)
    assert edits == [] and "load" not in rec.calls
    assert body(followups[0]) == text("catch.unavailable", reason="disabled")
    assert followups[0][1]["ephemeral"] is True


@pytest.mark.parametrize("kw", [{"creature": None}, {"pv": None}])
def test_a_missing_creature_says_so(monkeypatch, kw):
    _, edits, followups, _, _ = _click(monkeypatch, **kw)
    assert edits == [] and body(followups[0]) == text("creature.not_found")


def test_a_creature_that_cannot_evolve_any_more_is_refused_in_words(monkeypatch):
    from modules import catch_evolve as evo
    not_ready = ready_preview(owned_id=9, eligibility=evo.Eligibility("final_form", None))
    _, edits, followups, _, _ = _click(monkeypatch, pv=not_ready)
    assert edits == [] and body(followups[0]) == text("evolve.refused_final_form")


def test_a_database_failure_is_reported_and_the_button_can_be_used_again(monkeypatch):
    _, edits, followups, view, _ = _click(monkeypatch, boom=True)
    assert edits == [] and body(followups[0]) == text("levelup.error")
    assert view._busy is False


def test_a_second_tap_while_busy_is_ignored(monkeypatch):
    rec = Recorder()
    inter = make_interaction(rec)
    seen = []

    async def follow(*args, **kwargs):
        seen.append(shown(args, kwargs))

    inter.followup = SimpleNamespace(send=follow)
    view = lv.LevelUpEvolveView(7, None, 9)
    view._busy = True
    run(view._evolve(inter))
    assert seen == [text("creature.busy")] and rec.calls == ["response"]


def test_back_from_the_creature_screen_closes_the_stale_message(monkeypatch):
    rec = Recorder()
    inter = make_interaction(rec)
    got = []

    async def edit(*args, **kwargs):
        got.append(kwargs)

    inter.edit_original_response = edit
    run(lv.LevelUpEvolveView(7, None, 9)._closed(inter))
    assert got == [{"content": text("levelup.evolve_done"), "embed": None, "view": None, "attachments": []}]
