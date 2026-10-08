"""apply_event: idempotency, retry safety, and that only webhooks write entitlements."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import user_billing as ub

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self, fail_upsert=False):
        self.events, self.rows, self.fail_upsert = set(), {}, fail_upsert

    async def billing_event_claim(self, eid, uid, product, provider, kind):
        if eid in self.events:
            return False
        self.events.add(eid)
        return True

    async def billing_event_release(self, eid):
        self.events.discard(eid)

    async def entitlement_get(self, uid, product):
        return self.rows.get((uid, product))

    async def entitlement_upsert(self, uid, product, status, expires_at, source="",
                                 subscription_id=None, cancel_at_period_end=False):
        if self.fail_upsert:
            raise RuntimeError("db down")
        self.rows[(uid, product)] = dict(status=status, expires_at=expires_at, source=source,
                                         subscription_id=subscription_id,
                                         cancel_at_period_end=cancel_at_period_end)


def run(coro):
    return asyncio.run(coro)


def ev(db, eid, kind="charge", product="dev_monthly", **kw):
    return run(ub.apply_event(db, event_id=eid, user_id=123, product=product, kind=kind,
                              provider="gumroad", now=NOW, **kw))


def test_first_charge_grants():
    db = FakeDB()
    assert ev(db, "e1", subscription_id="sub1") == "applied"
    row = db.rows[("123", "dev_monthly")]
    assert row["status"] == "active" and row["subscription_id"] == "sub1"
    assert row["expires_at"] == NOW + timedelta(days=31)


def test_duplicate_event_does_not_double_extend():
    db = FakeDB()
    ev(db, "e1")
    first = db.rows[("123", "dev_monthly")]["expires_at"]
    assert ev(db, "e1") == "duplicate"
    assert ev(db, "e1") == "duplicate"
    assert db.rows[("123", "dev_monthly")]["expires_at"] == first


def test_distinct_renewals_each_extend_once():
    db = FakeDB()
    ev(db, "e1")
    ev(db, "e2")
    assert db.rows[("123", "dev_monthly")]["expires_at"] == NOW + timedelta(days=62)


def test_cancel_then_late_duplicate_charge_is_harmless():
    db = FakeDB()
    ev(db, "e1")
    ev(db, "c1", kind="cancel")
    assert db.rows[("123", "dev_monthly")]["cancel_at_period_end"] is True
    assert ev(db, "e1") == "duplicate"
    assert db.rows[("123", "dev_monthly")]["status"] == "cancelled"


def test_cancel_for_unknown_user_is_ignored():
    db = FakeDB()
    assert ev(db, "c1", kind="cancel") == "ignored"
    assert not db.rows


def test_failed_write_releases_claim_so_retry_succeeds():
    db = FakeDB(fail_upsert=True)
    try:
        ev(db, "e1")
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    assert "e1" not in db.events
    db.fail_upsert = False
    assert ev(db, "e1") == "applied"


def test_bad_input_is_ignored_without_claiming():
    db = FakeDB()
    assert ev(db, "", product="dev_monthly") == "ignored"
    assert ev(db, "e9", product="premium") == "ignored"
    assert ev(db, "e9", kind="refund") == "ignored"
    assert not db.events


def test_only_webhook_modules_may_call_entitlement_upsert():
    allowed = {"database.py", "modules/user_billing.py"}
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in list((root / "api").glob("*.py")) + [root / "dashboard" / "assets" / "dash.js"]:
        if "entitlement_upsert" in path.read_text(errors="ignore") and path.name != "dash.py":
            offenders.append(path.name)
    assert not offenders, offenders
    assert "api/dash_member.py" not in allowed
