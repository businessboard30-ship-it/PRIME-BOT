"""B4b: card background/logo uploads (pipeline, plan gate, moderation fail-safe, bot only draws approved)."""
import asyncio
import base64
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

import config
from api import dash
from modules import card_assets as ca, level_card_design as lcd
from tests.unit.test_dash_api import env, call  # noqa: F401

D = timedelta(days=1)


def img_bytes(size=(900, 300), color=(20, 30, 60), fmt="PNG", mode="RGB"):
    b = io.BytesIO()
    Image.new(mode, size, color).save(b, format=fmt)
    return b.getvalue()


def b64(raw):
    return base64.b64encode(raw).decode()


# ---------- pure pipeline ----------
def test_background_is_resized_and_reencoded_to_png():
    png, err = ca.process("background", img_bytes((1800, 600), fmt="JPEG"))
    assert err is None and Image.open(io.BytesIO(png)).size == (900, 300) and Image.open(io.BytesIO(png)).format == "PNG"


@pytest.mark.parametrize("raw", [b"", b"GIF89a....", b"not an image", b"<svg xmlns='x'/>", b"\x89PNG\r\n\x1a\n" + b"0" * 50])
def test_non_images_are_rejected(raw):
    assert ca.process("background", raw)[0] is None


def test_wrong_shape_too_small_and_too_big_are_rejected():
    assert ca.process("background", img_bytes((900, 900)))[0] is None            # not 3:1
    assert ca.process("background", img_bytes((300, 100)))[0] is None            # too small
    assert ca.process("background", b"0" * 1_600_000)[0] is None                 # over size cap
    assert ca.process("logo", img_bytes((32, 32)))[0] is None
    assert ca.process("nope", img_bytes())[0] is None


def test_gif_and_animation_are_rejected():
    b = io.BytesIO()
    Image.new("RGB", (900, 300)).save(b, format="GIF")
    assert ca.process("background", b.getvalue())[0] is None
    b = io.BytesIO()
    f = [Image.new("RGB", (900, 300), c) for c in ((0, 0, 0), (255, 0, 0))]
    f[0].save(b, format="PNG", save_all=True, append_images=f[1:])               # APNG
    assert ca.process("background", b.getvalue())[0] is None


def test_metadata_and_trailing_data_do_not_survive_reencode():
    raw = img_bytes() + b"<?php evil ?>SECRET"
    png, err = ca.process("background", raw)
    assert err is None and b"evil" not in png and b"SECRET" not in png


def test_logo_keeps_transparency_and_fits_box():
    png, err = ca.process("logo", img_bytes((1000, 800), (255, 0, 0, 0), mode="RGBA"))
    im = Image.open(io.BytesIO(png))
    assert err is None and im.mode == "RGBA" and im.size[0] <= 440 and im.size[1] <= 520


def test_b64_decode_is_strict():
    assert ca.decode_b64("!!notb64") is None and ca.decode_b64(5) is None and ca.decode_b64("A" * 4_000_001) is None
    assert ca.decode_b64(b64(b"hi")) == b"hi"


# ---------- fake db + moderation ----------
class _DB:
    def __init__(self):
        self.assets, self.blocked, self.plan = {}, set(), True
        self.rows = [{"product": "card_plan", "status": "active", "expires_at": datetime.now(timezone.utc) + 5 * D,
                      "cancel_at_period_end": False}]
        self.settings, self.design = {}, None

    async def entitlements_list(self, uid):
        return list(self.rows) if self.plan else []

    async def card_asset_get(self, uid, kind):
        return self.assets.get((uid, kind))

    async def card_asset_set(self, uid, kind, data, sha, status, reason):
        self.assets[(uid, kind)] = {"data": data, "sha256": sha, "status": status, "reason": reason}

    async def card_asset_status_set(self, uid, kind, sha, status, reason):
        a = self.assets.get((uid, kind))
        if a and a["sha256"] == sha:
            a["status"], a["reason"] = status, reason

    async def card_asset_delete(self, uid, kind):
        self.assets.pop((uid, kind), None)

    async def card_asset_blocked(self, sha):
        return sha in self.blocked

    async def card_asset_block(self, sha, reason=""):
        self.blocked.add(sha)

    async def user_card_get(self, uid):
        return self.design

    async def set_global_setting(self, k, v):
        self.settings[k] = v


def run(c):
    return asyncio.run(c)


def test_approved_upload_is_stored_and_status_is_returned(monkeypatch):
    db = _DB()

    async def ok(png):
        return {"status": "approved", "reason": ""}
    monkeypatch.setattr(ca, "moderate", ok)
    r = run(ca.submit(db, 6, "background", img_bytes()))
    assert r == {"ok": True, "status": "approved", "reason": ""} and db.assets[("6", "background")]["status"] == "approved"


def test_moderation_maps_openai_results_and_fails_safe(monkeypatch):
    def reply(obj=None, exc=None):
        async def f(png):
            if exc:
                raise exc
            return obj
        monkeypatch.setattr(ca, "_post_moderation", f)
    reply({"results": [{"flagged": False, "categories": {"sexual": False}}]})
    assert run(ca.moderate(b"x")) == {"status": "approved", "reason": ""}
    reply({"results": [{"flagged": True, "categories": {"sexual": True, "violence": False}}]})
    r = run(ca.moderate(b"x"))
    assert r["status"] == "rejected" and "sexual" in r["reason"] and "violence" not in r["reason"]
    for bad in ({}, {"results": []}, {"results": [{}]}, {"results": [{"flagged": "no"}]}, {"results": ["x"]}):
        reply(bad)
        assert run(ca.moderate(b"x"))["status"] == "pending"
    reply(exc=RuntimeError("OPENAI_API_KEY is not set"))
    assert run(ca.moderate(b"x"))["status"] == "pending"


def test_missing_key_means_pending_and_key_is_never_logged(monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert run(ca.moderate(b"x"))["status"] == "pending"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRETVALUE")
    import httpx

    class Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise httpx.ConnectError("sk-SECRETVALUE leaked?")
    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    with caplog.at_level("DEBUG"):
        assert run(ca.moderate(b"x"))["status"] == "pending"
    assert "SECRETVALUE" not in caplog.text


def test_request_goes_to_free_endpoint_with_image_input(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": [{"flagged": False}]}

    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            seen.update(url=url, headers=headers, body=json)
            return Resp()
    monkeypatch.setattr(httpx, "AsyncClient", C)
    assert run(ca.moderate(img_bytes()))["status"] == "approved"
    assert seen["url"] == "https://api.openai.com/v1/moderations" and seen["body"]["model"] == "omni-moderation-latest"
    assert seen["body"]["input"][0]["type"] == "image_url" and seen["body"]["input"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_rejected_image_hash_is_blocked_for_everyone(monkeypatch):
    db = _DB()

    async def rej(png):
        return {"status": "rejected", "reason": "nsfw"}
    monkeypatch.setattr(ca, "moderate", rej)
    raw = img_bytes()
    assert run(ca.submit(db, 6, "background", raw))["status"] == "rejected"
    again = run(ca.submit(db, 99, "background", raw))                              # other user, same image
    assert again["ok"] is False and ("99", "background") not in db.assets


# ---------- bot side: only approved is drawn ----------
def test_bot_only_uses_approved_assets_and_only_with_the_flag(monkeypatch):
    db = _DB()
    db.design = json.dumps({"custom_bg": True, "logo": True, "accent": "#112233"})
    bg, logo = img_bytes(), ca.process("logo", img_bytes((200, 200), (255, 0, 0, 255), mode="RGBA"))[0]
    db.assets[("6", "background")] = {"data": bg, "status": "pending", "sha256": "a", "reason": ""}
    db.assets[("6", "logo")] = {"data": logo, "status": "approved", "sha256": "b", "reason": ""}
    d, gbg, glogo = run(lcd.card_for_user(db, 6))
    assert gbg is None and glogo == logo                                           # pending bg never drawn
    db.assets[("6", "background")]["status"] = "rejected"
    assert run(lcd.card_for_user(db, 6))[1] is None
    db.design = json.dumps({"custom_bg": False, "logo": False})
    assert run(lcd.card_for_user(db, 6))[1:] == (None, None)                       # flag off -> not drawn
    db.plan = False
    assert run(lcd.card_for_user(db, 6)) is None                                   # lapsed plan -> default card


def test_render_with_assets_and_bright_background_gets_a_dark_text_panel():
    white = img_bytes(color=(255, 255, 255))
    logo = ca.process("logo", img_bytes((300, 300), (255, 0, 0, 255), mode="RGBA"))[0]
    d = {"custom_bg": True, "logo": True}
    png = lcd.render_custom_level_card(lcd.placeholder_avatar(), "Tester", 5, 10, 100, lcd.validate(d)[0], white, logo)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert im.size == (900, 300)
    assert sum(im.getpixel((300, 8))) < 300 or sum(im.getpixel((470, 20))) < 400   # text zone was darkened
    assert im.getpixel((770, 150))[0] > 200 and im.getpixel((770, 150))[1] < 80     # logo drawn on the right
    assert sum(im.getpixel((880, 290))) > 600                                      # rest of the bright bg untouched


def test_render_survives_corrupt_assets():
    d = lcd.validate({"custom_bg": True, "logo": True})[0]
    assert lcd.render_custom_level_card(lcd.placeholder_avatar(), "T", 1, 1, 10, d, b"junk", b"junk")[:4] == b"\x89PNG"


def test_design_flags_must_be_booleans():
    assert lcd.validate({"custom_bg": "yes"})[0] is None and lcd.validate({"logo": 1})[0] is None


# ---------- routes ----------
@pytest.fixture
def web(env, monkeypatch):
    fake, _ = env
    db = _DB()
    for n in ("entitlements_list", "card_asset_get", "card_asset_set", "card_asset_status_set", "card_asset_delete",
              "card_asset_blocked", "card_asset_block", "user_card_get", "set_global_setting"):
        monkeypatch.setattr(fake, n, getattr(db, n), raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://api.example.test", raising=False)

    async def usage(uid, ws, src):
        return 0
    monkeypatch.setattr(fake, "ai_usage_get", usage, raising=False)

    async def ok(png):
        return {"status": "approved", "reason": ""}
    monkeypatch.setattr(ca, "moderate", ok)
    dash._owner_hits.clear()
    return db


def test_upload_needs_session_and_plan(web):
    body = {"action": "member_card_asset", "kind": "background", "data": b64(img_bytes())}
    assert call("POST", token=None, body=body)[0] == 401
    web.plan = False
    st, p, _ = call("POST", body=body)
    assert st == 402 and p["checkout_url"].startswith("https://api.example.test/pay?t=") and not web.assets


def test_upload_with_plan_stores_for_session_user_and_ignores_client_ids(web):
    st, p, _ = call("POST", body={"action": "member_card_asset", "kind": "background", "data": b64(img_bytes()),
                                  "user_id": "999", "uid": "999"})
    assert st == 200 and p["status"] == "approved"
    assert list(web.assets) == [("6", "background")]


def test_bad_uploads_are_422(web):
    assert call("POST", body={"action": "member_card_asset", "kind": "x", "data": b64(img_bytes())})[0] == 422
    assert call("POST", body={"action": "member_card_asset", "kind": "background", "data": "%%%"})[0] == 422
    assert call("POST", body={"action": "member_card_asset", "kind": "background", "data": b64(b"nope")})[0] == 422
    assert not web.assets


def test_delete_only_touches_own_asset(web):
    web.assets[("6", "logo")] = {"data": b"x", "status": "approved", "sha256": "a", "reason": ""}
    web.assets[("999", "logo")] = {"data": b"x", "status": "approved", "sha256": "a", "reason": ""}
    assert call("POST", body={"action": "member_card_asset_delete", "kind": "logo", "user_id": "999"})[0] == 200
    assert ("6", "logo") not in web.assets and ("999", "logo") in web.assets


def test_member_card_reports_status_and_prompt_without_image_bytes(web):
    web.assets[("6", "background")] = {"data": b"SECRETBYTES", "status": "pending", "sha256": "a", "reason": "Waiting for a check."}
    st, p, _ = call("GET", {"action": "member_card"})
    assert st == 200 and p["assets"]["background"]["status"] == "pending" and p["assets"]["logo"] is None
    assert "{idea}" in p["prompt"] and "SECRETBYTES" not in json.dumps(p)


def test_preview_uses_own_pending_upload_but_not_rejected(web):
    web.assets[("6", "background")] = {"data": img_bytes(color=(255, 0, 0)), "status": "pending", "sha256": "a", "reason": ""}
    st, p, _ = call("POST", body={"action": "member_card_preview", "design": {"custom_bg": True}})
    assert st == 200
    im = Image.open(io.BytesIO(base64.b64decode(p["image"].split(",")[1]))).convert("RGB")
    assert im.getpixel((860, 290))[0] > 200
    web.assets[("6", "background")]["status"] = "rejected"
    st, p, _ = call("POST", body={"action": "member_card_preview", "design": {"custom_bg": True}})
    im = Image.open(io.BytesIO(base64.b64decode(p["image"].split(",")[1]))).convert("RGB")
    assert im.getpixel((860, 290))[0] < 100


def test_upload_is_rate_limited(web):
    body = {"action": "member_card_asset", "kind": "logo", "data": b64(img_bytes((200, 200), (1, 2, 3, 255), mode="RGBA"))}
    codes = [call("POST", body=body)[0] for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429


def test_big_body_limit_applies_only_to_the_upload_action():
    src = Path(dash.__file__).read_text()
    assert 'query.get("action", [""])[0] == "member_card_asset"' in src and 'headers.get("Authorization")' in src
    assert dash.MAX_BODY == 64 * 1024 and dash.MAX_UPLOAD_BODY == 3 * 1024 * 1024


def test_editor_front_end_has_no_innerhtml():
    js = (Path(__file__).resolve().parents[2] / "dashboard/assets/dash.js").read_text()
    seg = js[js.index("function renderCardEditor"):js.index("function renderMe")]
    assert "innerHTML" not in seg
