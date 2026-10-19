import asyncio
import inspect
from types import SimpleNamespace

from modules import ops_reports


class FakeConn:
    def __init__(self, db): self.db = db
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def fetchval(self, sql, *a):
        return 1 if self.db["recent"] else None
    async def execute(self, sql, *a):
        if sql.startswith("INSERT"): self.db["rows"].append(a)
    async def fetch(self, sql, *a):
        return self.db["pending"]


class FakePool:
    def __init__(self, db): self.db = db
    def acquire(self): return FakeConn(self.db)


def _patch(monkeypatch, db):
    async def gp(): return FakePool(db)
    monkeypatch.setattr(ops_reports, "_pool", gp)


def test_submit_queues_and_labels_clone(monkeypatch):
    db = {"recent": False, "rows": []}
    _patch(monkeypatch, db)
    assert asyncio.run(ops_reports.submit(SimpleNamespace(clone_id=4), "k", "hi")) is True
    assert db["rows"][0][0] == "clone #4"
    assert asyncio.run(ops_reports.submit(SimpleNamespace(clone_id=None), "k", "hi"))
    assert db["rows"][1][0] == "main bot"


def test_submit_respects_cooldown(monkeypatch):
    db = {"recent": True, "rows": []}
    _patch(monkeypatch, db)
    assert asyncio.run(ops_reports.submit(SimpleNamespace(clone_id=None), "k", "hi", cooldown=60)) is False
    assert db["rows"] == []


def test_flush_posts_to_support_channel_and_marks_posted(monkeypatch):
    db = {"recent": False, "rows": [], "pending": [{"id": 1, "bot_label": "clone #2", "text": "hello"}]}
    _patch(monkeypatch, db)
    sent = []

    class Ch:
        id = 5
        async def send(self, text, **kw): sent.append(text)

    async def ensure(bot): return Ch()
    monkeypatch.setattr(ops_reports, "ensure_channel", ensure)
    assert asyncio.run(ops_reports.flush(object())) == 1
    assert sent == ["**[clone #2]**\nhello"]


def test_flush_without_channel_posts_nothing(monkeypatch):
    db = {"recent": False, "rows": [], "pending": [{"id": 1, "bot_label": "x", "text": "t"}]}
    _patch(monkeypatch, db)

    async def ensure(bot): return None
    monkeypatch.setattr(ops_reports, "ensure_channel", ensure)
    assert asyncio.run(ops_reports.flush(object())) == 0


def test_bulk_care_buttons_acknowledge_before_the_slow_work():
    from discord_bot.cogs import _views_cat_adopt as v
    src = inspect.getsource(v.CatView._bulk) if hasattr(v, "CatView") else ""
    if not src:
        for name, obj in vars(v).items():
            if inspect.isclass(obj) and "_bulk" in vars(obj):
                src = inspect.getsource(obj._bulk)
    assert src
    assert src.index("response.defer()") < src.index("bulk_care(")
    assert "response.edit_message" not in src and "response.send_message" not in src
