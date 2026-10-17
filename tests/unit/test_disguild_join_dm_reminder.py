"""One-time "list your server on Disguild" reminder on the join DM: shown once per server EVER, survives redeploys, no repeats."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.components import _component_factory

from discord_bot import bot as bot_mod
from discord_bot.cogs import _views_join_dm as jd
from modules import guildatlas as ga

URL = "https://disguild.pages.dev/submit/server?utm_source=join-dm"
OFFER = {"show_listing": False, "show_invite": False, "show_disguild": True, "disguild_url": URL}
KEYS = list(jd.FEATURE_TOGGLES.keys())
PAGES = -(-len(KEYS) // jd.FEATURES_PER_PAGE)


def run(c):
    return asyncio.run(c)


def _links(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button) and c.style == discord.ButtonStyle.link]


def _disguild_links(view):
    return [b for b in _links(view) if ga.is_join_dm_list_url(b.url)]


def _view(page=0, offer=OFFER, **kw):
    return jd.build_join_dm_view(1, None, KEYS, page, intro="intro", notices=[("A", "b"), ("C", "d")], join_offer=offer, guild_name="G", **kw)


def _as_message(view):
    """What Discord hands back on a click: the sent view's components in wire form."""
    return SimpleNamespace(components=[_component_factory(d) for d in view.to_components()])


# ── the view ────────────────────────────────────────────────────────────────
def test_button_is_on_every_page_when_claimed():
    for page in range(PAGES):
        found = _disguild_links(_view(page))
        assert len(found) == 1 and found[0].label == "List your server on Disguild" and found[0].url == URL


def test_no_button_without_the_claim():
    for offer in (None, {"show_listing": True, "show_invite": True}, {**OFFER, "show_disguild": False}, {"show_disguild": True}):
        for page in range(PAGES):
            assert _disguild_links(_view(page, offer)) == []


def test_worst_case_page_stays_inside_discords_component_limit():
    ad = {"company_name": "Co", "ad_title": "T", "ad_description": "D https://x.y", "target_url": "https://x.y", "image_url": "https://x.y/i.png"}
    both = {"show_listing": True, "show_invite": True, "show_disguild": True, "disguild_url": URL}
    for page in range(PAGES):
        assert len(list(_view(page, both, fresh_ad=ad).walk_children())) <= 40


def test_offer_state_reads_the_button_back_off_a_live_message():
    state = jd._offer_state_from_message(_as_message(_view(0)))
    assert state["show_disguild"] is True and state["disguild_url"] == URL
    assert jd._offer_state_from_message(_as_message(_view(0, None)))["show_disguild"] is False


def test_paging_keeps_the_button_and_never_invents_one():
    for start in (0, 1):
        state = jd._offer_state_from_message(_as_message(_view(start)))
        assert len(_disguild_links(_view(1 - start, state))) == 1
    state = jd._offer_state_from_message(_as_message(_view(0, None)))
    assert _disguild_links(_view(1, state)) == []


def test_clicking_a_feature_button_keeps_the_link():
    rebuilt = jd._RebuiltCopyView(_as_message(_view(0)).components, lambda cid, comp: None)
    assert len(_disguild_links(rebuilt)) == 1


def test_link_urls():
    assert ga.join_dm_list_url("https://x.y/") == "https://x.y/submit/server?utm_source=join-dm"
    assert ga.is_join_dm_list_url(ga.join_dm_list_url("https://x.y"))
    assert not ga.is_join_dm_list_url("https://x.y/submit/server") and not ga.is_join_dm_list_url(None)
    assert ga.submit_url("bot", "https://x.y") == "https://x.y/submit/bot"


# ── the send path, with a fake database that remembers claims like Postgres does ─────────
class FakeDb:
    def __init__(self, dismissed=False):
        self.claimed, self.dismissed = set(), dismissed

    async def is_join_dm_dismissed(self, guild_id, clone_id):
        return self.dismissed

    async def claim_auto_listing_offer_send(self, guild_id, clone_id):
        return True

    async def claim_disguild_list_reminder(self, guild_id):
        if guild_id in self.claimed:
            return False
        self.claimed.add(guild_id)
        return True

    async def mark_quickstart_dm_sent(self, *a, **k):
        pass


def _send(db, monkeypatch, *, dm_fails=False, initial=True, guild_id=777):
    owner = SimpleNamespace(bot=False, send=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="x"), "closed") if dm_fails else None))
    channel = SimpleNamespace(send=AsyncMock())
    content = {"has_quickstart_cog": False, "feature_keys": KEYS, "intro": "i", "title": "t", "notices": []}

    async def build_view(guild, clone_id, content, join_offer=None, dm=False, compact=False):
        return jd.build_join_dm_view(guild.id, clone_id, content["feature_keys"], 0, intro="i", join_offer=join_offer, guild_name=guild.name)

    me = SimpleNamespace(clone_id=None, _build_join_dm_content=AsyncMock(return_value=content), _build_join_dm_view=build_view)
    monkeypatch.setattr(bot_mod, "db", db)
    monkeypatch.setattr(bot_mod, "resolve_guild_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(jd, "_default_text_channel", lambda guild: channel)
    run(bot_mod.AnimeBotDiscord._send_combined_owner_join_dm(me, SimpleNamespace(id=guild_id, name="G"), is_initial_send=initial))
    return owner, channel


def _sent_links(mock):
    return [len(_disguild_links(call.kwargs["view"])) for call in mock.call_args_list]


def test_first_join_shows_it_once_in_the_dm_and_not_in_the_server(monkeypatch):
    db = FakeDb()
    owner, channel = _send(db, monkeypatch)
    assert _sent_links(owner.send) == [1] and _sent_links(channel.send) == [0]
    assert db.claimed == {777}


def test_a_redeploy_or_duplicate_join_never_repeats_it(monkeypatch):
    db = FakeDb()
    _send(db, monkeypatch)
    for _ in range(3):  # three more "deploys": the same Postgres row is still there
        owner, channel = _send(db, monkeypatch)
        assert _sent_links(owner.send) == [0] and _sent_links(channel.send) == [0]


def test_reminder_loop_resends_do_not_show_or_claim_it(monkeypatch):
    db = FakeDb()
    owner, channel = _send(db, monkeypatch, initial=False)
    assert _sent_links(owner.send) == [0] and db.claimed == set()


def test_closed_dms_fall_back_to_the_server_copy(monkeypatch):
    db = FakeDb()
    owner, channel = _send(db, monkeypatch, dm_fails=True)
    assert _sent_links(channel.send) == [1]


def test_owner_who_opted_out_does_not_burn_the_claim(monkeypatch):
    db = FakeDb(dismissed=True)
    owner, channel = _send(db, monkeypatch)
    assert owner.send.call_count == 0 and db.claimed == set()


def test_each_server_gets_its_own_single_reminder(monkeypatch):
    db = FakeDb()
    for gid in (1, 2, 3):
        owner, _ = _send(db, monkeypatch, guild_id=gid)
        assert _sent_links(owner.send) == [1]
    assert db.claimed == {1, 2, 3}


def test_the_claim_is_a_persistent_insert_once_row():
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[2].joinpath("database.py").read_text(encoding="utf-8")
    assert re.search(r"CREATE TABLE IF NOT EXISTS disguild_list_reminder\s*\(\s*guild_id BIGINT PRIMARY KEY", src)
    assert "INSERT INTO disguild_list_reminder (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING RETURNING guild_id" in src
