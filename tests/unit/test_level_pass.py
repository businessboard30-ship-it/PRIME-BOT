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

    async def entitlement_start_trial(self, uid, days):
        if "card_plan" in self.ent:                                     # same rule as the SQL: insert-only, never overwrite
            return False
        self.ent["card_plan"] = {"product": "card_plan", "status": "active", "source": "trial",
                                 "expires_at": datetime.now(timezone.utc) + timedelta(days=days), "cancel_at_period_end": False}
        return True

    def used_up(self):
        """A member who already had the plan and whose time ran out: no trial offer, the pass offer instead."""
        self.ent["card_plan"] = {"product": "card_plan", "status": "expired", "source": "t",
                                 "expires_at": datetime.now(timezone.utc) - timedelta(days=3), "cancel_at_period_end": False}
        return self

    async def activate_xp_boost(self, guild_id, user_id, mult, days, clone_id=None, stack=False):
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
    assert config.LEVEL_PASS_CARD_DAYS == 45 and user_subs.PLANS["card_plan"]["period_days"] == 31   # monthly plan unchanged
    assert gp.expected_price_usd("level_pass") == 1.99
    assert auto.PRODUCTS["level_pass"]["kind"] == "one_time" and auto._cents("level_pass") == 199
    assert "level_pass" in config.GUMROAD_PRODUCT_LINKS and "level_pass" in config.GUMROAD_PRODUCT_IDS
    assert "level_pass" in pm.UNLOCK_HANDLERS


def test_unlock_grants_card_and_one_boost(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pm, "db", fake)
    run(pm._unlock_level_pass("ref1", 7, 555, None))
    card = fake.ent["card_plan"]
    assert card["status"] == "active" and timedelta(days=44) < card["expires_at"] - datetime.now(timezone.utc) <= timedelta(days=45, minutes=1)
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


def _offered(monkeypatch):
    async def offered(clone):
        return True
    monkeypatch.setattr(boost, "pass_offered", offered)


def test_edits_are_free_and_saving_needs_the_pass(monkeypatch):
    nopass = with_db(monkeypatch, FakeDB().used_up())
    _offered(monkeypatch)
    draft = run(vlc.draft_for(7, 1))
    i = Inter()
    run(vlc.edit_draft(i, draft, accent="#ff0000", font="clean"))
    assert draft.design["accent"] == "#FF0000" and draft.design["font"] == "clean" and draft.dirty
    assert i.edits and i.edits[0]["attachments"] and not nopass.cards                       # previewed, nothing stored
    j = Inter()
    run(vlc.save_draft(j, draft))
    assert not nopass.cards and j.private[0]["embed"].title == "🎫 Level Pass" and j.private[0]["ephemeral"] is True
    paid = with_db(monkeypatch, FakeDB(pass_days=10))
    k = Inter()
    run(vlc.save_draft(k, draft))
    saved = json.loads(paid.cards["7"])
    assert saved["accent"] == "#FF0000" and saved["font"] == "clean" and k.edits
    bad = Inter()
    run(vlc.edit_draft(bad, draft, background="nope"))
    assert bad.private and "Unknown background" in bad.private[0]["args"][0] and draft.design["background"] == "slate"


def test_the_designer_opens_for_everyone(monkeypatch):
    with_db(monkeypatch, FakeDB())
    i = Inter()
    run(vlc.open_card_wizard(i))
    emb = i.private[0]["embed"]
    assert emb.title.startswith("🎨") and i.private[0]["file"] and "Preview mode" in emb.description
    with_db(monkeypatch, FakeDB(pass_days=5))
    j = Inter()
    run(vlc.open_card_wizard(j))
    assert "Level Pass active" in j.private[0]["embed"].description and j.private[0]["ephemeral"] is True


def _png(size=(900, 300)):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, (20, 60, 120)).save(b, format="PNG")
    return b.getvalue()


def test_an_upload_is_only_staged_until_the_pass_holder_saves(monkeypatch):
    fake = with_db(monkeypatch, FakeDB())
    _offered(monkeypatch)
    sent, submitted = [], []

    async def fake_submit(db, uid, kind, raw):
        submitted.append(kind)
        return {"ok": True, "status": "approved", "reason": ""}

    async def fake_host(inter, kind):
        sent.append(kind)
    monkeypatch.setattr(card_assets, "submit", fake_submit)
    monkeypatch.setattr(vlc, "_send_to_host", fake_host)
    draft = run(vlc.draft_for(7, 1))

    def upload(kind, content_type="image/png", size=1000, data=b"x"):
        i = Inter()

        class Att:
            pass
        att = Att()
        att.content_type, att.size = content_type, size

        async def read(): return data
        att.read = read

        async def go():
            m = vlc.UploadModal(kind, draft)
            m.file._values = [att]
            await m.on_submit(i)
        run(go())
        return i

    bad = upload("background", content_type="image/gif")
    assert "PNG or JPEG" in bad.response.sent[0]["args"][0] and not draft.staged
    big = upload("logo", size=card_assets.MAX_BYTES["logo"] + 1)
    assert "too big" in big.response.sent[0]["args"][0] and not draft.staged
    assert card_assets.MAX_BYTES["background"] == 8 * 1024 * 1024                          # same limit as the welcome card
    ok = upload("background", data=_png())
    assert "background" in draft.staged and draft.design["custom_bg"] is True and ok.edits
    assert not submitted and not fake.cards                                                  # nothing stored yet
    fake.used_up()
    run(vlc.save_draft(Inter(), draft))                                                      # no pass: pitch only
    assert not submitted and not fake.cards
    paid = with_db(monkeypatch, FakeDB(pass_days=5))
    run(vlc.save_draft(Inter(), draft))
    assert submitted == ["background"] and sent == ["background"] and json.loads(paid.cards["7"])["custom_bg"] is True


def test_wired_in():
    assert re.match(lb.LeaderboardCardButton.__discord_ui_compiled_template__.pattern, "lvllb_card:1:-")
    assert lb.LeaderboardCardButton in lb.DYNAMIC_ITEMS
    spec = next(s for s in allow.AI_COMMANDS if s.name == "levelcard")
    assert spec.requires_confirmation is False and spec.cog_module == "discord_bot.cogs.leveling"
    assert 'name="levelcard"' in (ROOT / "discord_bot/cogs/leveling.py").read_text(encoding="utf-8")
    page_src = (ROOT / "discord_bot/cogs/_views_leveling_leaderboard.py").read_text(encoding="utf-8")
    assert "boost_row.add_item(LeaderboardCardButton(guild.id, clone_id))" in page_src            # My card is on the leaderboard page


async def _children(v):
    return v.children


# ── the $1 single boost is no longer sold ────────────────────────────────────
def test_the_one_dollar_boost_is_gone(monkeypatch):
    async def never(clone):
        return False
    monkeypatch.setattr(boost, "pass_offered", never)
    monkeypatch.setattr(boost, "_available_bundles", lambda: {})
    i = Inter()
    run(boost.BoostXPButton(1, None).callback(i))
    assert i.response.sent[0]["ephemeral"] is True and "view" not in i.response.sent[0]       # nothing on sale: no $1 fallback

    async def yes(clone):
        return True
    monkeypatch.setattr(boost, "pass_offered", yes)
    monkeypatch.setattr(boost, "_available_bundles", lambda: dict(config.XP_BOOST_BUNDLES))
    j = Inter()
    run(boost.BoostXPButton(1, None).callback(j))
    import discord as _d
    picker = j.response.sent[0]["view"]
    assert isinstance(picker, _d.ui.LayoutView) and picker.timeout is None
    nodes = []
    def walk(n):
        nodes.append(n)
        for c in getattr(n, "children", []) or []:
            walk(c)
    for n in picker.children:
        walk(n)
    text = "\n".join(n.content for n in nodes if isinstance(n, _d.ui.TextDisplay))
    assert "Level Pass" in text and "1 boost" not in text and "$1 " not in text
    labels = [n.item.label for n in nodes if isinstance(n, boost.BoostBuyButton)]
    assert labels[0].startswith("Level Pass") and not any(l.startswith("1 boost") for l in labels)


def test_terms_no_longer_list_the_one_dollar_boost():
    src = (ROOT / "api/legal_pages.py").read_text(encoding="utf-8")
    assert "XP_BOOST_FEE_USD" not in src and "Level Pass" in src
