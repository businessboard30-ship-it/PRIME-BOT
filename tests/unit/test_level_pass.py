"""Level Pass ($1.99): one payment = custom level-up card + one XP boost; Gumroad auto-create; the Discord designer."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import config
import gumroad_autocreate as auto
import gumroad_payments as gp
import payments_manual as pm
from discord_bot.cogs import _views_level_card as vlc
from discord_bot.cogs import _views_leveling_boost as boost
from discord_bot.cogs import _views_leveling_leaderboard as lb
from modules import ai_command_allowlist as allow
from modules import card_assets, user_subs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class FakeDB:
    """In-memory stand-in for the few db methods the pass touches."""

    def __init__(self, pass_days=None):
        self.claims, self.ent, self.boosts, self.cards, self.assets, self.deleted = set(), {}, [], {}, {}, []
        if pass_days:
            self.ent["card_plan"] = {"product": "card_plan", "status": "active", "source": "t",
                                     "expires_at": datetime.now(timezone.utc) + timedelta(days=pass_days),
                                     "cancel_at_period_end": False}
        self.boost_fail = False

    async def billing_event_claim(self, event_id, *a):
        if event_id in self.claims:
            return False
        self.claims.add(event_id)
        return True

    async def billing_event_release(self, event_id):
        self.claims.discard(event_id)

    async def entitlement_get(self, uid, product):
        return self.ent.get(product)

    async def entitlement_upsert(self, uid, product, status, expires_at, source="", subscription_id=None, cancel_at_period_end=False):
        self.ent[product] = {"product": product, "status": status, "expires_at": expires_at, "source": source,
                             "cancel_at_period_end": cancel_at_period_end}

    async def entitlements_list(self, uid):
        return list(self.ent.values())

    async def activate_xp_boost(self, guild_id, user_id, mult, days, clone_id=None):
        if self.boost_fail:
            raise RuntimeError("boom")
        self.boosts.append((guild_id, user_id, mult, days))

    async def user_card_get(self, uid):
        return self.cards.get(uid)

    async def user_card_set(self, uid, raw):
        self.cards[uid] = raw

    async def card_asset_get(self, uid, kind):
        return self.assets.get((uid, kind))

    async def card_asset_delete(self, uid, kind):
        self.deleted.append(kind)
        self.assets.pop((uid, kind), None)


# ── money ────────────────────────────────────────────────────────────────────
def test_price_and_periods_line_up():
    assert config.LEVEL_PASS_FEE_USD == 1.99
    assert config.LEVEL_PASS_CARD_DAYS == user_subs.PLANS["card_plan"]["period_days"]
    assert gp.expected_price_usd("level_pass") == 1.99
    assert auto.PRODUCTS["level_pass"]["kind"] == "one_time" and auto._cents("level_pass") == 199
    assert "level_pass" in config.GUMROAD_PRODUCT_LINKS and "level_pass" in config.GUMROAD_PRODUCT_IDS
    assert "level_pass" in pm.UNLOCK_HANDLERS


def test_unlock_grants_card_and_one_boost(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pm, "db", fake)
    run(pm._unlock_level_pass("ref1", 7, 555, None))
    card = fake.ent["card_plan"]
    assert card["status"] == "active" and timedelta(days=30) < card["expires_at"] - datetime.now(timezone.utc) <= timedelta(days=31, minutes=1)
    assert fake.boosts == [(555, 7, config.XP_BOOST_MULTIPLIER, config.XP_BOOST_DURATION_DAYS)]


def test_unlock_without_server_still_gives_the_card(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pm, "db", fake)
    run(pm._unlock_level_pass("ref2", 7, None, None))
    assert fake.ent["card_plan"]["status"] == "active" and not fake.boosts


def test_retry_after_a_failed_boost_never_extends_the_card_twice(monkeypatch):
    fake = FakeDB()
    fake.boost_fail = True
    monkeypatch.setattr(pm, "db", fake)
    try:
        run(pm._unlock_level_pass("ref3", 7, 555, None))
        assert False, "should raise so the gateway retries"
    except RuntimeError:
        pass
    first = fake.ent["card_plan"]["expires_at"]
    fake.boost_fail = False
    run(pm._unlock_level_pass("ref3", 7, 555, None))        # the gateway's retry
    assert fake.ent["card_plan"]["expires_at"] == first and len(fake.boosts) == 1


def test_enable_publishes_the_draft():
    class Resp:
        def __init__(self, data): self.data = data
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self, content_type=None): return self.data

    class Sess:
        def __init__(self, data): self.data, self.calls = data, []
        def put(self, url, data=None):
            self.calls.append((url, data))
            return Resp(self.data)
    ok = Sess({"success": True})
    assert run(auto._enable(ok, "tok", "abc")) is True and ok.calls[0][0].endswith("/abc/enable")
    assert run(auto._enable(Sess({"success": False}), "tok", "abc")) is False
    assert run(auto._enable(ok, "tok", "")) is False


# ── the Boost XP button ──────────────────────────────────────────────────────
def test_pass_is_offered_only_when_it_can_be_bought(monkeypatch):
    async def mode(clone):
        return "gumroad"
    monkeypatch.setattr(boost, "app_config", config)
    import database
    monkeypatch.setattr(database.db, "get_payment_mode", mode, raising=False)
    monkeypatch.setattr(auto, "load_runtime", lambda *a, **k: asyncio.sleep(0))
    auto.RUNTIME.pop("level_pass", None)
    assert run(boost.pass_offered(None)) is False                      # Gumroad-only and no product yet: old flow
    auto.RUNTIME["level_pass"] = {"id": "x", "url": "https://x.gumroad.com/l/pass"}
    try:
        assert run(boost.pass_offered(None)) is True
    finally:
        auto.RUNTIME.pop("level_pass", None)


# ── the Discord designer ─────────────────────────────────────────────────────
class Resp:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def defer(self, **kw):
        self.deferred = True

    async def send_message(self, *a, **kw):
        self.sent.append(kw | {"args": a})


class Inter:
    def __init__(self, uid=7):
        self.user = SimpleNamespace(id=uid, display_name="Max")
        self.guild_id, self.client = 1, SimpleNamespace(clone_id=None)
        self.response, self.followup, self.edits, self.private = Resp(), self, [], []

    async def send(self, *a, **kw):
        self.private.append(kw | {"args": a})

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


def with_db(monkeypatch, fake):
    monkeypatch.setattr(vlc, "db", fake)
    return fake


def test_changes_need_the_pass_and_are_validated(monkeypatch):
    nopass = with_db(monkeypatch, FakeDB())
    i = Inter()
    run(vlc.apply_change(i, accent="#FF0000"))
    assert not nopass.cards and i.edits and "Level Pass" in i.edits[0]["embed"].title      # sent back to the pitch
    paid = with_db(monkeypatch, FakeDB(pass_days=10))
    j = Inter()
    run(vlc.apply_change(j, accent="#ff0000", font="clean"))
    saved = json.loads(paid.cards["7"])
    assert saved["accent"] == "#FF0000" and saved["font"] == "clean" and j.edits and j.edits[0]["attachments"]
    k = Inter()
    run(vlc.apply_change(k, background="nope"))
    assert k.private and "Unknown background" in k.private[0]["args"][0] and json.loads(paid.cards["7"])["accent"] == "#FF0000"


def test_open_shows_pitch_without_pass_and_designer_with_it(monkeypatch):
    with_db(monkeypatch, FakeDB())
    async def offered(clone):
        return True
    monkeypatch.setattr(boost, "pass_offered", offered)
    i = Inter()
    run(vlc.open_card_wizard(i))
    assert i.private[0]["embed"].title == "🎫 Level Pass" and i.private[0]["ephemeral"] is True
    with_db(monkeypatch, FakeDB(pass_days=5))
    j = Inter()
    run(vlc.open_card_wizard(j))
    assert j.private[0]["embed"].title.startswith("🎨") and j.private[0]["file"] and j.private[0]["ephemeral"] is True


def test_upload_rules(monkeypatch):
    paid = with_db(monkeypatch, FakeDB(pass_days=5))
    sent, submitted = [], []

    async def fake_submit(db, uid, kind, raw):
        submitted.append(kind)
        return {"ok": True, "status": "approved", "reason": ""}

    async def fake_host(inter, kind):
        sent.append(kind)
    monkeypatch.setattr(card_assets, "submit", fake_submit)
    monkeypatch.setattr(vlc, "_send_to_host", fake_host)

    def upload(kind, content_type="image/png", size=1000, data=b"x"):
        i = Inter()

        class Att:
            pass
        att = Att()
        att.content_type, att.size = content_type, size
        async def read(): return data
        att.read = read

        async def go():
            m = vlc.UploadModal(kind)
            m.file._values = [att]
            await m.on_submit(i)
        run(go())
        return i

    bad = upload("background", content_type="image/gif")
    assert not submitted and "PNG or JPEG" in bad.response.sent[0]["args"][0]
    big = upload("logo", size=card_assets.MAX_BYTES["logo"] + 1)
    assert not submitted and "too big" in big.response.sent[0]["args"][0]
    ok = upload("background")
    assert submitted == ["background"] and sent == ["background"]
    assert json.loads(paid.cards["7"])["custom_bg"] is True and ok.edits
    submitted.clear()
    with_db(monkeypatch, FakeDB())                                      # no pass: nothing is uploaded
    nopass = upload("logo")
    assert not submitted and nopass.edits


def test_wired_in():
    assert re.match(lb.LeaderboardCardButton.__discord_ui_compiled_template__.pattern, "lvllb_card:1:-")
    assert lb.LeaderboardCardButton in lb.DYNAMIC_ITEMS
    spec = next(s for s in allow.AI_COMMANDS if s.name == "levelcard")
    assert spec.requires_confirmation is False and spec.cog_module == "discord_bot.cogs.leveling"
    assert 'name="levelcard"' in (ROOT / "discord_bot/cogs/leveling.py").read_text(encoding="utf-8")
    panel = lb.build_xp_options_view(1, None)
    assert any(isinstance(c, lb.LeaderboardCardButton) for c in asyncio.run(_children(panel)))


async def _children(v):
    return v.children
