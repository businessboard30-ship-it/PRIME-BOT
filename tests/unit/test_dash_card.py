"""B4: custom level-up card (validation, render, entitlement gate, expiry fallback, weekly AI counter)."""
import asyncio
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

import config
from api import dash, dash_member
from modules import ai_usage, level_card_design as lcd
from tests.unit.test_dash_api import env, call  # noqa: F401

D = timedelta(days=1)
GOOD = {"background": "ocean", "accent": "#ff8800", "font": "clean", "shape": "rounded", "bio": "Hello there"}


def _row(status="active", exp=None):
    exp = exp or datetime.now(timezone.utc) + 5 * D
    return {"product": "card_plan", "status": status, "expires_at": exp, "cancel_at_period_end": False}


@pytest.fixture
def card(env, monkeypatch):
    fake, _ = env
    st = {"rows": [], "design": None, "settings": {}, "usage": {}}

    async def entitlements_list(uid):
        st["uid"] = uid
        return list(st["rows"])

    async def user_card_get(uid):
        return st["design"]

    async def user_card_set(uid, d):
        st["design"], st["saved_for"] = d, uid

    async def set_global_setting(k, v):
        st["settings"][k] = v

    async def ai_usage_get(uid, ws, src):
        return st["usage"].get((uid, ws, src), 0)
    for n, f in (("entitlements_list", entitlements_list), ("user_card_get", user_card_get),
                 ("user_card_set", user_card_set), ("set_global_setting", set_global_setting),
                 ("ai_usage_get", ai_usage_get)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://api.example.test", raising=False)
    dash._owner_hits.clear()
    return st


# ---------- validation ----------
def test_valid_design_roundtrips_and_normalises():
    d, err = lcd.validate(GOOD)
    assert err is None and d["accent"] == "#FF8800" and d["font"] == "clean"


@pytest.mark.parametrize("bad", [
    {"accent": "red"}, {"accent": "#12345"}, {"accent": "#12345G"}, {"accent": "url(x)"}, {"accent": 5},
    {"font": "../../etc/passwd"}, {"font": "Comic"}, {"shape": "star"}, {"background": "http://x/y.png"},
    {"bio": "x" * 61}, {"bio": 5}, "string", None, ["a"]])
def test_bad_designs_are_rejected(bad):
    d, err = lcd.validate(bad)
    assert d is None and err


def test_bio_control_chars_are_stripped_and_unknown_keys_dropped():
    d, _ = lcd.validate({"bio": "hi\u202e\x00there", "evil": "<script>"})
    assert d["bio"] == "hithere" and "evil" not in d


def test_render_produces_a_card_png_for_every_option():
    for bg in lcd.BACKGROUNDS:
        for font in lcd.FONTS:
            for shape in lcd.SHAPES:
                png = lcd.render_custom_level_card(lcd.placeholder_avatar(), "Tester", 12, 40, 100,
                                                   {"background": bg, "font": font, "shape": shape, "bio": "bio"})
                assert Image.open(io.BytesIO(png)).size == (900, 300)


def test_render_survives_garbage_avatar_and_unicode_name():
    png = lcd.render_custom_level_card(b"not an image", "テスト ✨", 3, 0, 0, GOOD)
    assert png[:4] == b"\x89PNG"


# ---------- routes ----------
def test_card_routes_need_a_session(card):
    assert call("GET", {"action": "member_card"}, token=None)[0] == 401
    assert call("POST", token=None, body={"action": "member_card_save", "design": GOOD})[0] == 401
    assert call("POST", token=None, body={"action": "member_card_preview", "design": GOOD})[0] == 401


def test_preview_is_open_without_a_plan_and_stores_nothing(card):
    st, p, _ = call("POST", body={"action": "member_card_preview", "design": GOOD})
    assert st == 200 and p["image"].startswith("data:image/png;base64,")
    assert card["design"] is None


def test_preview_rejects_invalid_designs(card):
    assert call("POST", body={"action": "member_card_preview", "design": {"accent": "nope"}})[0] == 422


def test_save_without_a_plan_is_402_with_checkout_link_and_stores_nothing(card):
    st, p, _ = call("POST", body={"action": "member_card_save", "design": GOOD})
    assert st == 402 and p["plans_path"] == "#/me/plans" and "checkout_url" not in p
    assert card["design"] is None and not card["settings"]


@pytest.mark.parametrize("rows", [
    [_row("expired")], [_row(exp=datetime.now(timezone.utc) - D)],
    [{"product": "dev_monthly", "status": "active", "expires_at": datetime.now(timezone.utc) + D, "cancel_at_period_end": False}]])
def test_expired_or_wrong_product_cannot_save(card, rows):
    card["rows"] = rows
    assert call("POST", body={"action": "member_card_save", "design": GOOD})[0] == 402
    assert card["design"] is None


def test_save_with_plan_stores_for_session_user_ignoring_client_ids(card):
    card["rows"] = [_row()]
    st, p, _ = call("POST", body={"action": "member_card_save", "design": GOOD, "user_id": "999", "uid": "999"})
    assert st == 200 and card["saved_for"] == "6"
    assert json.loads(card["design"])["accent"] == "#FF8800"


def test_save_validates_before_the_gate_does_anything(card):
    card["rows"] = [_row()]
    assert call("POST", body={"action": "member_card_save", "design": {"font": "x"}})[0] == 422
    assert card["design"] is None


def test_member_card_returns_options_and_ai_meter_only_with_access(card):
    st, p, _ = call("GET", {"action": "member_card"})
    assert st == 200 and p["access"] is False and "ai" not in p and p["design"]["background"] == "slate"
    card["rows"], card["design"] = [_row()], json.dumps(GOOD)
    st, p, _ = call("GET", {"action": "member_card", "user_id": "999"})
    assert p["access"] is True and p["ai"]["limit"] == 10 and p["design"]["font"] == "clean"
    assert card["uid"] == "6"


def test_save_is_rate_limited(card):
    card["rows"] = [_row()]
    codes = [call("POST", body={"action": "member_card_save", "design": GOOD})[0] for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


# ---------- bot-side: expiry fallback ----------
class _DB:
    def __init__(self, rows, design):
        self.rows, self.design = rows, design

    async def entitlements_list(self, uid):
        return self.rows

    async def user_card_get(self, uid):
        return self.design


def test_design_for_user_is_used_only_while_the_plan_is_effective():
    saved = json.dumps(GOOD)
    run = lambda db: asyncio.run(lcd.design_for_user(db, 6))
    assert run(_DB([_row()], saved))["background"] == "ocean"
    assert run(_DB([_row("expired")], saved)) is None                              # lapsed -> default card
    assert run(_DB([_row(exp=datetime.now(timezone.utc) - D)], saved)) is None
    assert run(_DB([_row("cancelled")], saved)) is not None                        # cancelled keeps access to period end
    assert run(_DB([], saved)) is None and run(_DB([_row()], None)) is None
    assert run(_DB([_row()], "{broken")) is None                                   # corrupt row never crashes the bot


def test_design_returns_when_plan_is_renewed():
    saved = json.dumps(GOOD)
    assert asyncio.run(lcd.design_for_user(_DB([_row("expired")], saved), 6)) is None
    assert asyncio.run(lcd.design_for_user(_DB([_row()], saved), 6)) is not None


# ---------- weekly AI counter ----------
def test_week_boundary_is_monday_midnight_utc():
    wed = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
    assert ai_usage.week_start(wed).isoformat() == "2026-10-05"
    assert ai_usage.resets_at(wed) == datetime(2026, 10, 12, tzinfo=timezone.utc)
    sun = datetime(2026, 10, 11, 23, 59, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert ai_usage.week_start(sun) != ai_usage.week_start(mon)


def test_limits_are_separate_per_source_and_eleventh_is_refused():
    assert ai_usage.LIMITS == {"card_plan": 10, "dev": 50}

    class U:
        def __init__(self):
            self.n = {}

        async def ai_usage_consume(self, uid, ws, src, limit):
            k = (uid, ws, src)
            if self.n.get(k, 0) >= limit:
                return None
            self.n[k] = self.n.get(k, 0) + 1
            return self.n[k]

        async def ai_usage_get(self, uid, ws, src):
            return self.n.get((uid, ws, src), 0)
    u, now = U(), datetime(2026, 10, 7, tzinfo=timezone.utc)
    oks = [asyncio.run(ai_usage.consume(u, "6", "card_plan", now))[0] for _ in range(11)]
    assert oks == [True] * 10 + [False]
    assert asyncio.run(ai_usage.consume(u, "6", "dev", now))[0] is True            # dev counter untouched
    nxt = now + 7 * D
    assert asyncio.run(ai_usage.consume(u, "6", "card_plan", nxt))[0] is True      # new week resets


def test_no_discord_code_spends_the_website_allowance():
    root = Path(__file__).resolve().parents[2]
    for p in (root / "discord_bot").rglob("*.py"):
        t = p.read_text()
        assert not re.search(r"modules\.ai_usage|import ai_usage|\buser_ai_usage\b|ai_usage_consume|ai_usage_get", t), p


# ---------- hygiene ----------
def test_card_routes_never_read_a_user_id_from_the_client():
    src = Path(dash_member.__file__).read_text()
    assert not re.search(r'body\.get\(\s*["\'](user_id|uid)', src)
    assert "entitlement_upsert" not in src


def test_front_end_never_uses_innerhtml_for_the_editor():
    js = (Path(__file__).resolve().parents[2] / "dashboard/assets/dash.js").read_text()
    seg = js[js.index("function renderCardEditor"):js.index("function renderMe")]
    assert "innerHTML" not in seg
