import asyncio

from modules import cat_health


def setup_function(_):
    cat_health.reset()


def test_verdict_pool_only():
    cat_health.record("pool_manual", 5)
    v = cat_health.verdict(cat_health.snapshot(), 800, True, True)
    assert "NOT called" in v[0]


def test_verdict_api_failing_and_no_key():
    cat_health.record("api_http_error", error="HTTP 429")
    v = " ".join(cat_health.verdict(cat_health.snapshot(), 10, False, False))
    assert "failing" in v and "CAT_API_KEY is not set" in v


def test_verdict_hosting_problem():
    cat_health.record("api_ok", 2)
    cat_health.record("store_fail", 2)
    assert any("hosting channel" in x for x in cat_health.verdict(cat_health.snapshot(), 10, True, True))


def test_verdict_all_good():
    cat_health.record("api_call"); cat_health.record("api_ok"); cat_health.record("store_ok")
    assert cat_health.verdict(cat_health.snapshot(), 10, True, True) == ["No problem found: new pictures are arriving normally."]


def test_report_text_short_and_reset():
    cat_health.record("api_call", 3)
    t = cat_health.report_text(42, True, True, 6.0)
    assert "Pool size: 42" in t and "cat API calls made: 3" in t and len(t) < 1900
    cat_health.reset()
    assert cat_health.snapshot() == {}


def test_record_never_raises():
    cat_health.record(None)  # type: ignore[arg-type]


def test_send_report_dms_owners(monkeypatch):
    from modules import image_host
    got = {}

    async def fake(bot, key, text, cooldown=0):
        got.update(key=key, text=text)
        return True
    monkeypatch.setattr(image_host, "notify_owners", fake)
    from modules import cat_pool

    async def count():
        return 7
    monkeypatch.setattr(cat_pool, "pool_count", count)
    cat_health.record("pool_manual")
    assert asyncio.run(cat_health.send_report(object(), force=True)) is True
    assert got["key"] == "cat-health" and "Verdict" in got["text"]
    assert cat_health.snapshot() == {}
