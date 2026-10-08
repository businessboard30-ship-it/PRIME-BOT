"""Developer mode (#/dev): locked-by-default, server-side 402 on every dev_* route except dev_status."""
import inspect
import re

from api import dash, dash_dev
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  (session "sid" = user 6 with card_plan only; "other" = dev_monthly)


def test_dev_status_requires_a_session():
    dash._owner_hits.clear()
    assert call("GET", {"action": "dev_status"}, token=None)[0] == 401


def test_dev_status_is_open_but_locked_for_a_user_without_a_dev_plan(mem):
    st, p, _ = call("GET", {"action": "dev_status"})
    assert st == 200 and p["unlocked"] is False and p["expires_at"] is None
    assert [x["product"] for x in p["plans"]] == ["dev_monthly", "dev_yearly"]
    assert [x["price_usd"] for x in p["plans"]] == [5.0, 30.0]         # server prices, card plan is not listed
    assert all(x["state"] == "none" for x in p["plans"])


def test_card_plan_never_unlocks_developer_mode(mem):
    assert call("GET", {"action": "dev_status"})[1]["unlocked"] is False
    st, p, _ = call("GET", {"action": "dev_overview"})
    assert st == 402 and p["code"] == "subscription_required" and p["ok"] is False


def test_dev_subscriber_is_unlocked(mem):
    fake, seen = mem
    st, p, _ = call("GET", {"action": "dev_status"}, token="other")
    assert st == 200 and p["unlocked"] is True and p["expires_at"]
    st, p, _ = call("GET", {"action": "dev_overview"}, token="other")
    assert st == 200 and p["weekly_bot_chats"] == 50
    assert set(seen) == {"7777777777"}


def test_client_supplied_user_id_is_ignored(mem):
    fake, seen = mem
    st, p, _ = call("GET", {"action": "dev_overview", "user_id": "7777777777", "uid": "7777777777"})
    assert st == 402 and seen == ["6"]


def test_expired_plan_is_locked(mem):
    from datetime import datetime, timedelta, timezone
    fake, _ = mem

    async def lst(uid):
        return [{"product": "dev_monthly", "status": "active", "expires_at": datetime.now(timezone.utc) - timedelta(days=1)}]
    fake.entitlements_list = lst
    assert call("GET", {"action": "dev_overview"})[0] == 402
    st, p, _ = call("GET", {"action": "dev_status"})
    assert p["unlocked"] is False and p["export_available"] is True        # 7-day export grace


def test_dev_status_does_not_leak_internal_fields(mem):
    fake, _ = mem

    async def lst(uid):
        from datetime import datetime, timedelta, timezone
        return [{"product": "dev_yearly", "status": "active", "expires_at": datetime.now(timezone.utc) + timedelta(days=9),
                 "subscription_id": "sub_SECRET", "source": "gumroad"}]
    fake.entitlements_list = lst
    st, p, _ = call("GET", {"action": "dev_status"})
    assert "SECRET" not in str(p) and "gumroad" not in str(p)


def test_every_dev_route_except_status_is_gated():
    handlers = {**dash_dev.ROUTES, **dash_dev.WRITES}
    assert "dev_status" in handlers
    for name, fn in handlers.items():
        assert name.startswith("dev_")
        if name != "dev_status":
            assert "await require_dev(uid, db)" in inspect.getsource(fn), f"{name} must call require_dev first"


def test_dev_module_never_writes_entitlements_or_takes_a_user_id():
    src = inspect.getsource(dash_dev)
    assert "entitlement_upsert" not in src
    assert not re.search(r'q\(\s*["\'](user_id|uid)', src) and 'body.get("user_id")' not in src


def test_dev_page_is_always_routed_and_never_uses_innerhtml():
    from pathlib import Path
    js = Path("dashboard/assets/dash.js").read_text()
    assert 'hash === "#/dev"' in js and 'href: "#/dev"' in js
    page = js[js.index("function devChat"):js.index("function renderOwner")]
    assert "innerHTML" not in page and 'api("dev_status")' in page
    assert "startCheckout(" in page         # Subscribe goes through the shared server-priced checkout loader


def test_checkout_loader_is_shared_recoverable_and_safe():
    from pathlib import Path
    js = Path("dashboard/assets/dash.js").read_text()
    css = Path("dashboard/assets/dash.css").read_text()
    fn = js[js.index("function startCheckout"):js.index("function renderMe")]
    assert js.count('api("checkout_user"') == 1 and js.count("startCheckout(p.product") == 2      # /me and /dev share it
    assert "ringsArt()" in fn and "innerHTML" not in fn
    assert "pageshow" in js and "endCheckoutUi" in js and "Cancel" in fn                        # not stuck after Back / slow start
    assert r'/^https:\/\//' in fn                                                              # only follows an https checkout link
    assert ".payload{" in css
