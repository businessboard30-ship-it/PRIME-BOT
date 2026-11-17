import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from modules import payment_apology as pa

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(minutes_ago):
    return (datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_prompt_unlock_gets_no_apology():
    assert pa.minutes_late({"sale_timestamp": iso(2)}, NOW) is None
    assert pa.minutes_late({"sale_timestamp": iso(19)}, NOW) is None


def test_a_late_unlock_is_apologised_for_with_the_delay():
    assert pa.minutes_late({"sale_timestamp": iso(20)}, NOW) == 20
    assert pa.minutes_late({"sale_timestamp": iso(180)}, NOW) == 180


def test_no_or_unreadable_sale_time_never_apologises():
    for bad in (None, "", "soon", 0, -3, True):
        assert pa.minutes_late({"sale_timestamp": bad}, NOW) is None
    assert pa.minutes_late({}, NOW) is None


def test_checkout_age_is_irrelevant():
    # the buyer opened the pay link long ago but only paid just now: the ping is prompt, so no apology
    assert pa.minutes_late({"sale_timestamp": iso(1), "created_at": iso(500)}, NOW) is None


def test_epoch_formats_are_understood():
    assert pa.minutes_late({"sale_timestamp": str(int(NOW) - 3600)}, NOW) == 60
    assert pa.minutes_late({"sale_timestamp": str((int(NOW) - 3600) * 1000)}, NOW) == 60


@pytest.mark.parametrize("minutes,needle", [(45, "about 45 minutes"), (180, "about 3 hours"), (4000, "about 3 days")])
def test_the_note_says_how_long_it_took(minutes, needle):
    t = pa.apology_note(minutes)
    assert needle in t and "glitch" in t and "Sorry" in t and "active" in t


# ── wired into the Gumroad success path ───────────────────────────────────

def _confirm(monkeypatch, fields):
    """Runs a full successful Gumroad ping and returns the DMs the buyer got."""
    import gumroad_payments as gp
    import payments_manual
    dms = []
    row = {"user_id": 5, "payment_type": "premium", "status": "pending", "provider": gp.PROVIDER,
           "chat_id": 99, "clone_id": None}

    async def _row(ref): return row
    async def _noload(*a, **k): return None
    async def _valid(*a, **k): return True
    async def _handler(*a, **k): return None
    async def _dm(user_id, clone_id, text): dms.append(text)

    class Conn:
        async def fetchrow(self, *a, **k): return {"claimed": True}
        async def execute(self, *a, **k): return None

    class Acquire:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acquire()

    async def _pool(): return Pool()

    monkeypatch.setattr(gp.db, "get_payment_by_reference", _row)
    monkeypatch.setattr(gp._auto, "load_runtime", _noload)
    monkeypatch.setattr(gp, "_sale_is_valid", _valid)
    monkeypatch.setattr(gp, "_matches_product", lambda t, f: True)
    monkeypatch.setattr(gp, "get_pool", _pool)
    monkeypatch.setattr(gp, "_dm", _dm)
    monkeypatch.setitem(payments_manual.UNLOCK_HANDLERS, "premium", _handler)
    out = asyncio.run(gp._process_gumroad_ping_inner(dict({"reference": "gum_1", "price": "999999", "sale_id": "s1"}, **fields)))
    return out, dms


def test_late_sale_buyer_gets_confirmation_plus_apology(monkeypatch):
    out, dms = _confirm(monkeypatch, {"sale_timestamp": (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert out == (200, "ok") and len(dms) == 1
    assert "confirmed and applied" in dms[0] and "small glitch" in dms[0] and "about 3 hours" in dms[0]


def test_prompt_sale_buyer_gets_only_the_confirmation(monkeypatch):
    out, dms = _confirm(monkeypatch, {"sale_timestamp": (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert out == (200, "ok") and len(dms) == 1
    assert "confirmed and applied" in dms[0] and "glitch" not in dms[0]


def test_sale_without_a_timestamp_gets_only_the_confirmation(monkeypatch):
    out, dms = _confirm(monkeypatch, {})
    assert out == (200, "ok") and "glitch" not in dms[0]
