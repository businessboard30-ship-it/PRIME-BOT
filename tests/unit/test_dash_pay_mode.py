"""Dashboard payments: Paystack and Gumroad buttons that follow the bot's payment mode."""
import json
from pathlib import Path

import pytest

import config
from api import dash
from modules import user_subs
from tests.unit.test_dash_checkout_user import cdb  # noqa: F401
from tests.unit.test_dash_api import call, env  # noqa: F401


def test_pay_options_follow_the_mode():
    names = lambda m: [o["provider"] for o in user_subs.pay_options(m)]
    assert names("split") == ["paystack", "gumroad"]
    assert names("auto") == ["paystack"]
    assert names("gumroad") == ["gumroad"]
    assert names("manual") == names(None) == names("junk") == ["paystack", "gumroad"]


def _mode(cdb, monkeypatch, mode):
    async def gm(clone_id=None):
        return mode
    monkeypatch.setattr(cdb, "get_payment_mode", gm, raising=False)


@pytest.mark.parametrize("mode,expect", [("split", 2), ("auto", 1), ("gumroad", 1)])
def test_plans_and_dev_status_list_buttons_for_the_mode(cdb, monkeypatch, mode, expect):
    _mode(cdb, monkeypatch, mode)
    assert len(call("GET", {"action": "member_plans"})[1]["pay"]) == expect
    assert len(call("GET", {"action": "dev_status"})[1]["pay"]) == expect


@pytest.fixture
def made(monkeypatch):
    seen = []

    async def fake(intent, country):
        seen.append(country)
        return "https://checkout.example.test/" + ("paystack" if country == "GH" else "gumroad")
    import payments_manual
    monkeypatch.setattr(payments_manual, "_create_user_plan_checkout", fake)
    return seen


def _buy(provider=None, product="dev_monthly"):
    body = {"action": "checkout_user", "product": product}
    if provider:
        body["provider"] = provider
    return call("POST", {}, body=body)


def test_split_checkout_goes_straight_to_the_chosen_gateway(cdb, monkeypatch, made):
    _mode(cdb, monkeypatch, "split")
    p = _buy("paystack")[1]
    assert p["checkout_url"] == "https://checkout.example.test/paystack" and "/pay?" not in p["checkout_url"]
    assert _buy("gumroad")[1]["checkout_url"] == "https://checkout.example.test/gumroad"
    assert made == ["GH", "XX"]
    st, p, _ = _buy()                                           # split with no choice: ask, never guess
    assert st == 422 and len(p["pay"]) == 2


def test_single_gateway_modes_use_it_and_refuse_the_other(cdb, monkeypatch, made):
    _mode(cdb, monkeypatch, "auto")
    assert _buy("gumroad")[0] == 422
    assert _buy()[1]["checkout_url"].endswith("/paystack")
    _mode(cdb, monkeypatch, "gumroad")
    assert _buy("paystack")[0] == 422
    assert _buy()[1]["checkout_url"].endswith("/gumroad")


def test_gateway_failure_is_a_clean_502(cdb, monkeypatch):
    import payments_manual

    async def none(intent, country):
        return None
    monkeypatch.setattr(payments_manual, "_create_user_plan_checkout", none)
    _mode(cdb, monkeypatch, "gumroad")
    assert _buy()[0] == 502


def test_dashboard_never_goes_through_slash_pay():
    src = Path("api/dash_member.py").read_text()
    assert "/pay" not in src.replace("# ", "") .split("async def checkout_user")[1].split("async def member_card(")[0].replace("no /pay hop", "").replace("no /pay", "")


def test_pay_redirect_enforces_the_mode_for_plans():
    src = Path("api/pay_redirect.py").read_text()
    assert "is_plan(intent.get" in src and 'override = "gh"' in src and 'override = "intl"' in src


def test_member_pages_use_the_admin_shell_and_no_innerhtml():
    js = Path("dashboard/assets/dash.js").read_text()
    page = js[js.index("function renderMemberShell"):js.index("function renderOwner")]
    assert 'class: "shell"' in page and 'class: "nav"' in page and "innerHTML" not in page
    assert "Pay with" not in page                                # labels come from the server, not hardcoded
