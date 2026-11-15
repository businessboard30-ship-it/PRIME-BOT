import asyncio
import time

from modules import scam_vision as sv


def test_quota_429_pauses_for_hours():
    body = '{"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}'
    secs, quota = sv._openai_429_pause(None, body, 0)
    assert quota and secs >= 3600


def test_rate_limit_429_backs_off_and_caps():
    assert sv._openai_429_pause(None, "rate limit", 0) == (60, False)
    assert sv._openai_429_pause(None, "rate limit", 1) == (120, False)
    assert sv._openai_429_pause(None, "rate limit", 3) == (480, False)
    assert sv._openai_429_pause(None, "rate limit", 10) == (900, False)


def test_rate_limit_honours_retry_after_within_bounds():
    assert sv._openai_429_pause(45.0, "", 5) == (45, False)
    assert sv._openai_429_pause(5.0, "", 0)[0] == 30      # floor
    assert sv._openai_429_pause(99999.0, "", 0)[0] == 900  # cap


class _Resp:
    def __init__(self, status, text="", headers=None, js=None):
        self.status_code, self.text, self.headers, self._js = status, text, headers or {}, js or {}

    def json(self):
        return self._js


def _patch_client(monkeypatch, resp):
    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return resp
    monkeypatch.setattr(sv.httpx, "AsyncClient", _C)
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def test_ask_openai_quota_blocks_long_and_reports(monkeypatch):
    sv._s.o_blocked_until, sv._s.o_429_streak = 0.0, 0
    _patch_client(monkeypatch, _Resp(429, '{"error":{"code":"insufficient_quota"}}'))
    assert asyncio.run(sv._ask_openai(b"x")) is None
    assert sv._s.o_blocked_until - time.monotonic() > 3000
    assert "quota" in sv._s.o_last_detail and sv._s.o_429_streak == 0


def test_ask_openai_rate_limit_streak_grows_then_resets_on_success(monkeypatch):
    sv._s.o_blocked_until, sv._s.o_429_streak = 0.0, 0
    _patch_client(monkeypatch, _Resp(429, "Rate limit reached"))
    asyncio.run(sv._ask_openai(b"x"))
    asyncio.run(sv._ask_openai(b"x"))
    assert sv._s.o_429_streak == 2
    ok = _Resp(200, js={"choices": [{"message": {"content": "NOT_SCAM"}}]})
    _patch_client(monkeypatch, ok)
    asyncio.run(sv._ask_openai(b"x"))
    assert sv._s.o_429_streak == 0
