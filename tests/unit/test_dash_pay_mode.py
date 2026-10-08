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


def _intent(cdb, p):
    return json.loads(cdb.settings["payintent:" + p["checkout_url"].split("t=")[1].split("&")[0]])


def test_split_checkout_routes_to_the_chosen_gateway(cdb, monkeypatch):
    _mode(cdb, monkeypatch, "split")
    p = call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly", "provider": "paystack"})[1]
    assert p["checkout_url"].endswith("&r=gh") and p["provider"] == "paystack"
    p = call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly", "provider": "gumroad"})[1]
    assert p["checkout_url"].endswith("&r=intl")
    p = call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly"})[1]
    assert "&r=" not in p["checkout_url"]                      # no choice: the /pay page detects the country


def test_single_gateway_modes_refuse_the_other_one(cdb, monkeypatch):
    _mode(cdb, monkeypatch, "auto")
    assert call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly", "provider": "gumroad"})[0] == 422
    p = call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly"})[1]
    assert p["checkout_url"].endswith("&r=gh")                 # the only gateway is used by default
    _mode(cdb, monkeypatch, "gumroad")
    assert call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly", "provider": "paystack"})[0] == 422
    assert call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly"})[1]["checkout_url"].endswith("&r=intl")


def test_pay_redirect_enforces_the_mode_for_plans():
    src = Path("api/pay_redirect.py").read_text()
    assert "is_plan(intent.get" in src and 'override = "gh"' in src and 'override = "intl"' in src


def test_member_pages_use_the_admin_shell_and_no_innerhtml():
    js = Path("dashboard/assets/dash.js").read_text()
    page = js[js.index("function renderMemberShell"):js.index("function renderOwner")]
    assert 'class: "shell"' in page and 'class: "nav"' in page and "innerHTML" not in page
    assert "Pay with" not in page                                # labels come from the server, not hardcoded
