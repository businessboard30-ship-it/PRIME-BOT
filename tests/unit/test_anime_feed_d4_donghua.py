"""Anime feed D4: donghua. Per-server 'what to post' setting (default both), origin filter, multi-page airing fetch. No network."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from modules import anime_feed as af
from modules import anime_feed_worker as aw

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def P(i, origin="JP", at=0):
    return af.Preview(i, f"Show {i}", 1, at, "s", None, f"https://anilist.co/anime/{i}", (), "", None, None, None, None, origin)


def row(i, origin, at=100):
    return {"airingAt": at + i, "episode": 1, "media": {"id": i, "isAdult": False, "title": {"english": f"T{i}"}, "countryOfOrigin": origin}}


def payload(rows, nxt=False):
    return {"data": {"Page": {"pageInfo": {"hasNextPage": nxt}, "airingSchedules": rows}}}


def test_default_is_both_and_unknown_values_fall_back():
    assert af.FeedConfig(1).content_kinds == "both"
    assert af.clean_kinds(None) == "both" and af.clean_kinds("junk") == "both" and af.clean_kinds("donghua") == "donghua"


def test_kind_filter():
    items = [P(1, "JP"), P(2, "CN"), P(3, ""), P(4, "KR")]
    assert [p.anime_id for p in af.filter_kinds(items, "both")] == [1, 2, 3, 4]
    assert [p.anime_id for p in af.filter_kinds(items, "anime")] == [1, 3, 4]      # only Chinese animation is excluded
    assert [p.anime_id for p in af.filter_kinds(items, "donghua")] == [2]


def test_old_rows_without_the_column_read_as_both():
    cfg = af._cfg(1, None, {"enabled": True, "channel_id": 5, "enabled_at": None, "ping_role_id": None})
    assert cfg.content_kinds == "both"
    cfg = af._cfg(1, None, {"enabled": True, "channel_id": 5, "enabled_at": None, "ping_role_id": None, "content_kinds": "anime"})
    assert cfg.content_kinds == "anime"


def test_has_next_page_never_raises():
    assert af.has_next_page(payload([], True)) and not af.has_next_page(payload([])) and not af.has_next_page({}) and not af.has_next_page(None)


def test_airing_soon_reads_later_pages_so_donghua_are_not_crowded_out(monkeypatch):
    calls = []

    async def fake(now, page=1):
        calls.append(page)
        return payload([row(1, "JP")], True) if page == 1 else payload([row(2, "CN")], page < 3) if page == 2 else payload([row(3, "CN"), row(1, "JP")])

    monkeypatch.setattr(af, "_fetch", fake)
    monkeypatch.setattr(af, "_cache", (0.0, []))
    items, ok = run(af.airing_soon())
    assert ok and calls == [1, 2, 3]
    assert sorted(p.anime_id for p in items) == [1, 2, 3]            # duplicate of 1 on page 3 dropped
    assert af.MAX_PAGES == 3


def test_a_failing_later_page_keeps_what_page_one_gave(monkeypatch):
    async def fake(now, page=1):
        return payload([row(1, "JP")], True) if page == 1 else None

    monkeypatch.setattr(af, "_fetch", fake)
    monkeypatch.setattr(af, "_cache", (0.0, []))
    items, ok = run(af.airing_soon())
    assert ok and [p.anime_id for p in items] == [1]


def test_worker_posts_only_what_the_server_wants():
    now = 10_000_000
    items = [P(1, "JP", now - 60), P(2, "CN", now - 60)]
    for kinds, want in (("both", [1, 2]), ("anime", [1]), ("donghua", [2])):
        assert [p.anime_id for p in aw.due(af.filter_kinds(items, kinds), now)] == want


def test_migration_default_is_both_additive_and_wired():
    sql = (ROOT / "database/migrations/075_anime_feed_kinds.sql").read_text()
    assert "content_kinds TEXT NOT NULL DEFAULT 'both'" in sql and "IF NOT EXISTS" in sql
    assert "DROP " not in sql.upper() and "DELETE " not in sql.upper()
    assert "075_anime_feed_kinds.sql" in (ROOT / "database.py").read_text()


def test_wizard_has_the_setting_and_says_it_on_screen():
    from discord_bot.cogs import _views_anime_feed as v
    assert set(v.KIND_LABELS) == set(af.KINDS)
    assert "Anime and donghua" in v.setup_text(af.FeedConfig(1))
    assert "Donghua only" in v.setup_text(af.FeedConfig(1, content_kinds="donghua"))
