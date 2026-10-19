"""/cat + join DM: Cat button on page 1, Applications/Partnership/Advertise moved to "More features"."""
import asyncio

import discord
import pytest
from discord import app_commands

from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import cat


def run(coro):
    return asyncio.run(coro)


def ids(view):
    return [getattr(c, "custom_id", None) for c in view.walk_children() if getattr(c, "custom_id", None)]


def labels_of_last_row(page, keys=None):
    keys = keys or list(jd.FEATURE_TOGGLES)
    view = jd.build_join_dm_view(1, clone_id=None, feature_keys=keys, page=page, intro="x")
    rows = [c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)]
    return [getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None) for c in rows[-1].children]


# ---------- fake HTTP ----------
class Resp:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    """Maps a URL prefix to a Resp (or an Exception to raise on get)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        for prefix, r in self.routes.items():
            if url.startswith(prefix):
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(url)


@pytest.fixture(autouse=True)
def clean():
    cat._recent.clear()
    cat._last_use.clear()
    yield


GOOD = "https://cdn2.thecatapi.com/images/abc.jpg"


async def _false():
    return False


# ---------- picture sources ----------
def test_thecatapi_first():
    s = Session({cat.CAT_API_URL: Resp(200, [{"url": GOOD}])})
    assert run(cat.fetch_cat_url(s)) == GOOD and len(s.calls) == 1


def test_api_key_sent_only_when_configured(monkeypatch):
    monkeypatch.setattr(cat.config, "CAT_API_KEY", "k123", raising=False)
    s = Session({cat.CAT_API_URL: Resp(200, [{"url": GOOD}])})
    run(cat.fetch_cat_url(s))
    assert s.calls[0][1] == {"x-api-key": "k123"}
    monkeypatch.setattr(cat.config, "CAT_API_KEY", "", raising=False)
    s2 = Session({cat.CAT_API_URL: Resp(200, [{"url": GOOD}])})
    run(cat.fetch_cat_url(s2))
    assert not s2.calls[0][1]


def test_no_other_service_is_called_when_the_cat_api_fails():
    for first in (Resp(503, None), RuntimeError("boom"), Resp(200, {"nope": 1}), Resp(200, [])):
        s = Session({cat.CAT_API_URL: first})
        assert run(cat.fetch_cat_url(s)) is None
        assert [c[0] for c in s.calls] == [cat.CAT_API_URL]        # one call, to The Cat API only


def test_unsafe_urls_are_never_returned():
    bad = ["http://cdn2.thecatapi.com/x.jpg", "https://evil.example/x.jpg", "https://thecatapi.com.evil.io/x.jpg",
           "javascript:alert(1)", "https://thecatapi.com@evil.io/x", "https://cataas.com/cat/1", ""]
    for url in bad:
        assert not cat.safe_image_url(url), url
    s = Session({cat.CAT_API_URL: Resp(200, [{"url": "https://evil.example/x.jpg"}])})
    assert run(cat.fetch_cat_url(s)) is None
    assert cat.safe_image_url(GOOD)


def test_cataas_is_gone_from_the_cat_cog():
    import inspect
    src = inspect.getsource(cat).lower()
    assert "cataas" not in src
    assert not hasattr(cat, "CATAAS_URL") and not hasattr(cat, "_from_cataas") and not hasattr(cat, "_candidates_cataas")
    assert cat.ALLOWED_HOSTS == ("thecatapi.com",)


def test_recent_urls_used_when_the_service_is_down():
    run(cat.fetch_cat_url(Session({cat.CAT_API_URL: Resp(200, [{"url": GOOD}])})))
    down = Session({cat.CAT_API_URL: RuntimeError("x")})
    assert run(cat.fetch_cat_url(down)) == GOOD
    cat._recent.clear()
    assert run(cat.fetch_cat_url(down)) is None


def test_cooldown_is_per_person():
    assert cat.cooldown_left(1) == 0
    assert 0 < cat.cooldown_left(1) <= cat.COOLDOWN_SECONDS
    assert cat.cooldown_left(2) == 0


def test_embed_has_image():
    assert cat.cat_embed(GOOD).image.url == GOOD


# ---------- slash command slot safety ----------
def test_setup_survives_command_limit():
    class Bot:
        async def add_cog(self, c):
            raise app_commands.CommandLimitReached(None, 100)
    run(cat.setup(Bot()))                       # must not raise


def test_setup_registers_cat_and_catsetup_in_separate_cogs():
    added = []

    class Bot:
        async def add_cog(self, c):
            added.append(c)
    run(cat.setup(Bot()))
    assert [[c.name for c in cog.get_app_commands()] for cog in added] == [["cat"], ["catsetup"]]


def test_a_full_command_list_keeps_cat_when_only_one_slot_is_left():
    added = []

    class Bot:
        async def add_cog(self, c):
            if isinstance(c, cat.CatSetupCog):
                raise app_commands.CommandLimitReached(None, 100)
            added.append(c)
    run(cat.setup(Bot()))                       # must not raise, /cat still registered
    assert [c.name for c in added[0].get_app_commands()] == ["cat"]


def _interaction(can_manage=True, in_text_channel=True):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    ch = MagicMock(spec=discord.TextChannel); ch.id = 7; ch.mention = "#c"
    ch.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=in_text_channel)
    guild = SimpleNamespace(id=1, me=object(), owner_id=5 if can_manage else 6, system_channel=None, text_channels=[ch],
                            get_member=lambda uid: None)
    i = MagicMock()
    i.guild, i.channel, i.user = guild, ch, SimpleNamespace(id=5)
    i.response.send_message, i.response.defer, i.followup.send = AsyncMock(), AsyncMock(), AsyncMock()
    return i, ch


def test_catsetup_opens_the_same_wizard_as_the_cat_button(monkeypatch):
    from unittest.mock import AsyncMock
    from discord_bot.cogs import _views_cat_setup as wiz
    opened = AsyncMock()
    monkeypatch.setattr(wiz, "open_setup", opened)
    c = cat.CatSetupCog(type("B", (), {"clone_id": 3})())
    i, ch = _interaction()
    run(c.catsetup.callback(c, i))
    opened.assert_awaited_once()
    args = opened.await_args.args
    assert args[1] is i.guild and args[2] == 3 and args[3] == 5 and args[4] is ch
    assert "Cat setup opened" in i.followup.send.await_args.args[0]


def test_catsetup_needs_manage_server(monkeypatch):
    from unittest.mock import AsyncMock
    from discord_bot.cogs import _views_cat_setup as wiz
    opened = AsyncMock()
    monkeypatch.setattr(wiz, "open_setup", opened)
    c = cat.CatSetupCog(object())
    i, _ = _interaction(can_manage=False)
    async def no(guild, uid, permission="manage_guild"): return False
    import discord_bot.cogs._views_shared as shared
    monkeypatch.setattr(shared, "user_can_manage_guild", no)
    run(c.catsetup.callback(c, i))
    opened.assert_not_awaited()
    assert "Manage Server" in i.response.send_message.await_args.args[0]


def test_catsetup_says_so_when_it_cannot_post_anywhere(monkeypatch):
    from unittest.mock import AsyncMock
    from discord_bot.cogs import _views_cat_setup as wiz
    monkeypatch.setattr(wiz, "open_setup", AsyncMock())
    c = cat.CatSetupCog(object())
    i, _ = _interaction(in_text_channel=False)
    run(c.catsetup.callback(c, i))
    wiz.open_setup.assert_not_awaited()
    assert "couldn't find a channel" in i.followup.send.await_args.args[0]


# ---------- join DM layout ----------
def test_first_page_row_has_more_honeypot_cat_profile_and_panel_last():
    labels = labels_of_last_row(0)
    assert labels == ["More features", "Honeypot", "Cat", "Edit profile", "Open server panel"]
    assert len(labels) <= 5          # Discord allows at most 5 buttons in one row


def test_first_page_no_longer_has_the_three_moved_buttons():
    keys = list(jd.FEATURE_TOGGLES)
    p1 = ids(jd.build_join_dm_view(5, None, keys, 0, intro="x"))
    for gone in ("join_dm_apply", "join_dm_bump", "join_dm_advertise"):
        assert not any(i.startswith(gone) for i in p1)
    assert "join_dm_more:5:-" in p1 and "join_dm_cat:5:-" in p1 and "join_dm_honeypot:5:-" in p1


def test_later_pages_unchanged():
    keys = list(jd.FEATURE_TOGGLES)
    p2 = ids(jd.build_join_dm_view(5, None, keys, 1, intro="x"))
    assert "join_dm_connect:5:-" in p2 and "join_dm_advertise:5:-" in p2
    assert "join_dm_cat:5:-" not in p2 and "join_dm_more:5:-" not in p2
    assert labels_of_last_row(1)[-1] != "Open server panel"


def test_more_screen_has_the_three_buttons_with_unchanged_ids_and_a_back():
    got = ids(jd.build_join_dm_more_view(7, 3))
    assert {"join_dm_apply:7:3", "join_dm_bump:7:3", "join_dm_advertise:7:3", "join_dm_more_back:7:3"} <= set(got)


def test_new_buttons_registered_with_unique_templates_that_do_not_overlap():
    items = (jd._MoreFeaturesButton, jd._MoreBackButton, jd._CatButton)
    assert all(i in jd.DYNAMIC_ITEMS for i in items)
    pats = [i.__discord_ui_compiled_template__.pattern for i in jd.DYNAMIC_ITEMS]
    assert len(pats) == len(set(pats))
    # each custom_id is claimed by exactly one dynamic item
    for cid in ("join_dm_more:7:-", "join_dm_more_back:7:3", "join_dm_cat:7:-"):
        hits = [i for i in jd.DYNAMIC_ITEMS if i.__discord_ui_compiled_template__.match(cid)]
        assert len(hits) == 1, (cid, hits)


@pytest.mark.parametrize("cls,cid,clone", [(jd._MoreFeaturesButton, "join_dm_more:7:-", None),
                                           (jd._MoreBackButton, "join_dm_more_back:7:3", 3),
                                           (jd._CatButton, "join_dm_cat:7:-", None)])
def test_buttons_round_trip_their_custom_id(cls, cid, clone):
    m = cls.__discord_ui_compiled_template__.match(cid)
    obj = run(cls.from_custom_id(None, None, m))
    assert obj.guild_id == 7 and obj.clone_id == clone and obj.item.custom_id == cid


# ---------- button behaviour ----------
class FakeInteraction:
    def __init__(self):
        self.user = type("U", (), {"id": 42})()
        self.guild_id = 7
        self.client = type("C", (), {"get_guild": lambda self, gid: object()})()
        self.sent, self.followups, self.edits, self._done = [], [], [], False
        self.response = self
        self.followup = self

    def is_done(self):
        return self._done

    async def defer(self, **k):
        self._done = True

    async def send_message(self, content=None, **k):
        self._done = True
        self.sent.append((content, k))

    async def send(self, content=None, **k):
        self.followups.append((content, k))

    async def edit_original_response(self, **k):
        self.edits.append(k)


def test_more_button_edits_the_message_into_the_more_screen():
    i = FakeInteraction()
    run(jd._MoreFeaturesButton(7, None).callback(i))
    assert isinstance(i.edits[0]["view"], jd.JoinDMMoreView)


def test_cat_button_sends_an_embed_then_rate_limits(monkeypatch):
    monkeypatch.setattr(jd, "user_can_manage_guild", lambda *a, **k: _false())
    async def fake(session=None, bot=None, guild_id=None):
        return GOOD
    monkeypatch.setattr(cat, "fetch_cat_url", fake)
    i = FakeInteraction()
    run(jd._CatButton(7, None).callback(i))
    assert GOOD in _v2_urls(i.followups[0][1]["view"])
    j = FakeInteraction()
    run(jd._CatButton(7, None).callback(j))                   # same user, straight away
    v = j.followups[0][1]["view"]                            # the menu still opens, just without a new picture
    assert "another cat picture" in _v2_text(v) and GOOD not in _v2_urls(v) and j.followups[0][1]["ephemeral"] is False


def test_cat_button_says_sorry_when_no_cat(monkeypatch):
    monkeypatch.setattr(jd, "user_can_manage_guild", lambda *a, **k: _false())
    async def none(session=None, bot=None, guild_id=None):
        return None
    monkeypatch.setattr(cat, "fetch_cat_url", none)
    i = FakeInteraction()
    run(jd._CatButton(7, None).callback(i))
    v = i.followups[0][1]["view"]                            # the menu opens with a note instead of a picture
    assert "couldn't find a cat" in _v2_text(v) and GOOD not in _v2_urls(v) and i.followups[0][1]["ephemeral"] is False


def _v2_text(view):
    out = []
    def walk(items):
        for it in items:
            if hasattr(it, "content"):
                out.append(it.content)
            walk(getattr(it, "children", []) or [])
    walk(view.children)
    return "\n".join(out)


def _v2_urls(view):
    return str(view.to_components())
