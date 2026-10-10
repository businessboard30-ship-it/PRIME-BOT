import asyncio
import pytest

import config
from modules import applications as apps


# ── application forms ─────────────────────────────────────────────────────

def test_colours_two_free_ten_premium():
    assert len(apps.COLORS) == 10
    assert sum(1 for c in apps.COLORS if not c[3]) == 2


def test_question_caps_by_tier():
    raw = "\n".join(f"Question {i}?" for i in range(8))
    assert len(apps.parse_questions(raw, premium=False)) == 3
    assert len(apps.parse_questions(raw, premium=True)) == 5
    assert apps.parse_questions("  \n\n a \n", premium=False) == ["a"]
    assert len(apps.parse_questions("x" * 300, premium=False)[0]) == apps.QUESTION_MAX_LEN


def test_emoji_validation():
    assert apps.clean_emoji("📝") == "📝"
    assert apps.clean_emoji("<:wave:123456789012345678>") == "<:wave:123456789012345678>"
    assert apps.clean_emoji("hello") is None
    assert apps.clean_emoji("") is None


def _form(**kw):
    f = dict(id=7, guild_id=1, clone_id=None, creator_id=2, title="📝 Staff", description="Apply!", button_label="Apply",
             button_emoji="📝", color_key="blurple", questions=["Why?"], post_channel_id=None, review_channel_id=None,
             accept_role_id=None, status="draft", panel_channel_id=None, panel_message_id=None)
    f.update(kw)
    return f


def _ids(view):
    out = []

    def walk(c):
        cid = getattr(c, "custom_id", None)
        if cid:
            out.append(cid)
        for ch in getattr(c, "children", []) or []:
            walk(ch)
    for c in view.children:
        walk(c)
    return out


def test_wizard_has_every_control_and_panel_has_apply_button():
    from discord_bot.cogs import _views_applications as va
    ids = _ids(va.build_wizard_view(_form(), premium=False))
    for field in ("details", "questions", "color", "postch", "revch", "role", "publish", "premium"):
        assert f"appw_{field}:7" in ids
    # Premium servers don't get the upsell button.
    assert "appw_premium:7" not in _ids(va.build_wizard_view(_form(), premium=True))
    assert "appl_apply:7" in _ids(va.build_panel_view(_form()))


def test_all_application_items_registered_with_unique_templates():
    from discord_bot.cogs import _views_applications as va
    patterns = [i.__discord_ui_compiled_template__.pattern for i in va.DYNAMIC_ITEMS]
    assert len(patterns) == len(set(patterns)) == 10


def test_review_buttons_only_while_pending():
    from discord_bot.cogs import _views_applications as va
    sub = dict(id=5, user_id=9, answers=[{"q": "Why?", "a": "because"}])

    class U:  # stand-in for a Member
        mention = "<@9>"
    assert {"appl_rev:5:a", "appl_rev:5:d"} <= set(_ids(va.build_review_view(_form(), sub, U())))
    assert not any(i.startswith("appl_rev") for i in _ids(va.build_review_view(_form(), sub, U(), result="✅ Accepted")))


# ── join DM: Apply replaces Connect on page 1, Connect moves to page 2 ────

def test_join_dm_apply_on_page_one_connect_on_page_two():
    from discord_bot.cogs import _views_join_dm as jd
    assert jd._ApplyFormButton in jd.DYNAMIC_ITEMS
    keys = list(jd.FEATURE_TOGGLES)
    p1 = _ids(jd.build_join_dm_view(123, None, keys, 0, intro="hi"))
    p2 = _ids(jd.build_join_dm_view(123, None, keys, 1, intro="hi"))
    assert "join_dm_apply:123:-" in p1 and "join_dm_connect:123:-" not in p1
    assert "join_dm_connect:123:-" in p2 and "join_dm_apply:123:-" not in p2
    # a one-page DM must not lose Connect
    one = _ids(jd.build_join_dm_view(123, None, keys[:3], 0, intro="hi"))
    assert "join_dm_connect:123:-" in one and "join_dm_apply:123:-" in one


# ── discount code ─────────────────────────────────────────────────────────

def test_discount_price_is_half_of_yearly():
    import premium_discount as pd
    assert pd.discounted_usd() == round(config.PREMIUM_YEARLY_FEE_USD / 2, 2)


def test_code_is_masked_and_unguessable_shape():
    import premium_discount as pd
    c1, c2 = pd._new_code(), pd._new_code()
    assert c1 != c2 and c1.startswith("PB50") and len(c1) == 14
    assert c1 not in pd.mask(c1) and pd.mask(c1).endswith(c1[-4:])


def test_gumroad_link_carries_code_only_when_given(monkeypatch):
    import gumroad_payments as gp
    monkeypatch.setattr(gp._auto, "link_for", lambda t: "https://x.gumroad.com/l/abc")
    plain = gp.build_link("premium_yearly", 1, "ref1")
    assert "code=" not in plain and "/l/abc?" in plain
    disc = gp.build_link("premium_yearly", 1, "ref1", discount_code="PB50XYZ")
    assert "/l/abc/PB50XYZ?" in disc and "code=PB50XYZ" in disc and "reference=ref1" in disc


def test_not_eligible_after_redeem_or_prior_payment(monkeypatch):
    import premium_discount as pd

    async def run(code_row, prior):
        async def _get(uid): return code_row
        async def _prior(uid): return prior
        monkeypatch.setattr(pd, "get_code", _get)
        monkeypatch.setattr(pd, "has_prior_premium_payment", _prior)
        return await pd.eligible(1)

    assert asyncio.run(run(None, False))[0] is True
    assert asyncio.run(run({"status": "issued"}, False))[0] is True
    assert asyncio.run(run({"status": "redeemed"}, False))[0] is False
    assert asyncio.run(run(None, True))[0] is False


def test_ping_accepts_discount_only_for_reserved_order(monkeypatch):
    """The 50% price is honoured only when the code was reserved for this exact reference + user."""
    import gumroad_payments as gp
    import premium_discount as pd

    calls = {}
    row = {"user_id": 5, "payment_type": "premium_yearly", "status": "pending", "provider": gp.PROVIDER,
           "chat_id": 99, "clone_id": None}

    async def _row(ref): return row
    async def _noload(*a, **k): return None
    async def _valid(*a, **k): return True

    async def _disc(ref, uid):
        calls["asked"] = (ref, uid)
        return {"code": "x"} if (ref, uid) == ("gum_ok", 5) else None

    monkeypatch.setattr(gp.db, "get_payment_by_reference", _row)
    monkeypatch.setattr(gp._auto, "load_runtime", _noload)
    monkeypatch.setattr(gp, "_sale_is_valid", _valid)
    monkeypatch.setattr(gp, "_matches_product", lambda t, f: True)

    async def _pool():
        raise RuntimeError("reached the claim step")
    monkeypatch.setattr(gp, "get_pool", _pool)
    monkeypatch.setattr(pd, "discount_for_reference", _disc)

    half = int(pd.discounted_usd() * 100)
    # Reserved order paying half → gets past the price check (then stops at the fake pool, which is fine)
    with pytest.raises(Exception) as exc:
        asyncio.run(gp._process_gumroad_ping_inner({"reference": "gum_ok", "price": str(half), "sale_id": "s"}))
    assert "reached the claim step" in str(exc.value)
    # A stolen code on any other order → underpaid, nothing unlocked
    code, msg = asyncio.run(gp._process_gumroad_ping_inner({"reference": "gum_other", "price": str(half), "sale_id": "s"}))
    assert (code, msg) == (200, "underpaid")


# ── persistent modals (survive restarts) ──────────────────────────────────

class _FakeInteraction:
    def __init__(self, data):
        self.data = data


def test_modal_values_reads_text_inputs_in_any_layout():
    from discord_bot.cogs import _views_applications as va
    rows = {"custom_id": "appl_modal:7", "components": [
        {"type": 1, "components": [{"type": 4, "custom_id": "q0", "value": "because"}]},
        {"type": 18, "component": {"type": 4, "custom_id": "q1", "value": "daily"}},
    ]}
    assert va.modal_values(_FakeInteraction(rows)) == {"q0": "because", "q1": "daily"}


def test_modals_have_fixed_ids_and_no_in_memory_state():
    from discord_bot.cogs import _views_applications as va
    f = _form(questions=["Why?", "How active?"])
    assert va._ApplyModal(f).custom_id == "appl_modal:7"
    assert va._DetailsModal(f).custom_id == "appm_details:7"
    assert va._QuestionsModal(f, True).custom_id == "appm_questions:7"
    assert [c.custom_id for c in va._ApplyModal(f).children] == ["q0", "q1"]


def test_modal_submit_is_routed_without_a_live_modal(monkeypatch):
    from discord_bot.cogs import _views_applications as va
    seen = {}

    async def _app(i, form_id, vals): seen["app"] = (form_id, vals)
    async def _det(i, form_id, vals): seen["det"] = (form_id, vals)
    monkeypatch.setattr(va, "_submit_application", _app)
    monkeypatch.setattr(va, "_submit_details", _det)
    data = {"custom_id": "appl_modal:7", "components": [{"type": 1, "components": [{"custom_id": "q0", "value": "hi"}]}]}
    assert asyncio.run(va.handle_modal_submit(_FakeInteraction(data))) is True
    assert seen["app"] == (7, {"q0": "hi"})
    data["custom_id"] = "appm_details:9"
    assert asyncio.run(va.handle_modal_submit(_FakeInteraction(data))) is True and seen["det"][0] == 9
    # someone else's modal is left alone
    assert asyncio.run(va.handle_modal_submit(_FakeInteraction({"custom_id": "other:1"}))) is False


# ── server panel: Community → Applications ────────────────────────────────

def _labels(view):
    out = []

    def walk(c):
        if getattr(c, "label", None):
            out.append(c.label)
        if getattr(c, "placeholder", None):
            out.append(c.placeholder)
        for ch in getattr(c, "children", []) or []:
            walk(ch)
    for c in view.children:
        walk(c)
    return out


def test_panel_community_hub_links_to_applications():
    from discord_bot.cogs import _views_server_panel_p2 as p2
    v = p2.CommunityView(1, None, 2, {"app_live": 2})
    assert "Applications" in _labels(v)


def test_panel_applications_screen_lists_forms_and_controls():
    from discord_bot.cogs import _views_server_panel_p2 as p2
    forms = [_form(id=1, status="active", title="Staff team", panel_channel_id=55, pending=3),
             _form(id=2, status="closed", title="Mods", pending=0)]
    v = p2.ApplicationsView(1, None, 2, {"forms": forms, "premium": False})
    labels = _labels(v)
    assert "New form in this channel" in labels and "Close or reopen a form" in labels and "Back" in labels
    body = "\n".join(v.body())
    assert "Live forms: **1/1**" in body and "3 waiting for review" in body and "<#55>" in body
    assert "Premium allows 10" in body
    empty = p2.ApplicationsView(1, None, 2, {"forms": [], "premium": True})
    assert "Close or reopen a form" not in _labels(empty)
    assert "Live forms: **0/10**" in "\n".join(empty.body())
