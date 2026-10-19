"""Phase 5: public cat posts (spawns, for-sale posts, announcements, shared profile cards) as Components V2.

Older posts are classic embed messages and stay in channels, so every edit must work on BOTH kinds of message.
A V2 message can only be edited with a V2 view and a classic one only with an embed; these tests cover each pairing.
"""
import asyncio
import datetime
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import _views_cat_features as vf
from discord_bot.cogs import _views_cat_hub as hub
from discord_bot.cogs import _views_cat_market as vm
from discord_bot.cogs import cat
from modules import cat_adopt as ca
from modules import cat_announce, cat_market as cm, cat_pool, cat_share
from tests.unit.test_cat_adopt import FI, mkcat


def run(coro):
    return asyncio.run(coro)


def _count(view) -> int:
    """Components Discord counts in one message (containers, rows, buttons, text, ... all nested)."""
    return sum(1 for _ in view.walk_children())


def _ids(view):
    return [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]


def _v2_message(view):
    """A stand-in for a V2 message as Discord sends it back: real component objects built from the payload."""
    flags = discord.MessageFlags()
    flags.components_v2 = True
    comps = [discord.components._component_factory(c) for c in view.to_components()]
    return SimpleNamespace(flags=flags, components=comps, embeds=[], edit=AsyncMock())


def _classic_message(title="🐾 Hi", image="https://cdn.discordapp.com/x.jpg"):
    e = discord.Embed(title=title, description="old text")
    e.set_image(url=image)
    return SimpleNamespace(flags=discord.MessageFlags(), components=[], embeds=[e], edit=AsyncMock())


NEXT = datetime.datetime(2026, 10, 19, 20, 40, tzinfo=datetime.timezone.utc)
PIC = "https://cdn.discordapp.com/x.jpg"


@pytest.fixture(autouse=True)
def no_images(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value=PIC))


# ── DynamicItems keep working inside V2 rows ───────────────────────────────────────────────────────────────────────

def test_persistent_buttons_in_a_v2_row_keep_their_ids_and_match_their_templates():
    view = v.spawn_post(31, "Cat", PIC, NEXT)
    assert _ids(view) == ["cat_adopt:31", "cat_mine", "cat_market", "cat_help", "cat_menu"]
    classes = {c.__discord_ui_compiled_template__.pattern: c for c in v.DYNAMIC_ITEMS}
    for cid in _ids(view):
        hits = [(pat, cls) for pat, cls in classes.items() if re.fullmatch(pat, cid)]
        assert len(hits) == 1, cid


def test_a_dynamic_item_is_rebuilt_from_a_v2_message_button_id():
    m = re.fullmatch(v.AdoptButton.__discord_ui_compiled_template__.pattern, "cat_adopt:31")
    item = run(v.AdoptButton.from_custom_id(SimpleNamespace(), None, m))
    assert isinstance(item, v.AdoptButton) and item.spawn_id == 31
    lm = re.fullmatch(vm.BuyButton.__discord_ui_compiled_template__.pattern, "cat_buy:9")
    assert run(vm.BuyButton.from_custom_id(SimpleNamespace(), None, lm)).listing_id == 9
    um = re.fullmatch(vm.UnlistButton.__discord_ui_compiled_template__.pattern, "cat_unlist:9")
    assert run(vm.UnlistButton.from_custom_id(SimpleNamespace(), None, um)).listing_id == 9


def test_the_registered_dynamic_items_cover_every_id_on_the_new_posts():
    names = {c.__name__ for c in v.DYNAMIC_ITEMS}
    assert {"AdoptButton", "MyCatsButton", "HelpButton", "MenuButton"} <= names


# ── spawn post ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_spawn_post_budget_and_content():
    view = v.spawn_post(31, "A cat", PIC, NEXT)
    assert 8 <= _count(view) <= 40 and _count(view) == 10      # container, text, picture, separator, row, 5 buttons
    text = v.view_text(view)
    assert text.startswith("## A cat") and "Next cat" in text and "<t:" in text
    assert any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())


def test_a_spawn_without_a_recorded_id_is_just_the_picture_card():
    view = v.spawn_post(None, "A cat", PIC)
    assert _ids(view) == [] and _count(view) == 3 and "Next cat" not in v.view_text(view)


def test_a_v2_spawn_message_is_read_back_with_its_title_picture_and_next_cat_lines():
    msg = _v2_message(v.spawn_post(31, "A cat", PIC, NEXT))
    heading, tail, image = v.spawn_parts(msg)
    assert heading == "## A cat" and tail.startswith("**⏰ Next cat**") and image == PIC


def test_edit_of_a_v2_spawn_gives_a_v2_card_and_never_an_embed():
    msg = _v2_message(v.spawn_post(31, "A cat", PIC, NEXT))
    kw = v.spawn_edit_kwargs(msg, 31, "💖 **Mochi** was adopted by <@10>!", adopted=True)
    assert set(kw) == {"view", "allowed_mentions"} and isinstance(kw["view"], discord.ui.LayoutView)
    assert kw["allowed_mentions"].users is False
    text = v.view_text(kw["view"])
    assert "## A cat" in text and "adopted by <@10>" in text and "Next cat" in text      # title and next-cat kept
    btn = [c for c in kw["view"].walk_children() if isinstance(c, v.AdoptButton)][0]
    assert btn.item.disabled and btn.item.label.startswith("Adopted")
    assert _ids(kw["view"]) == _ids(v.spawn_post(31, "x", PIC, NEXT))                    # same persistent buttons


def test_edit_of_an_old_classic_spawn_still_gives_an_embed_and_classic_buttons():
    kw = v.spawn_edit_kwargs(_classic_message(), 31, ca.EXPIRED_NOTE, gone=True)
    assert set(kw) == {"embed", "view"} and kw["embed"].description == ca.EXPIRED_NOTE
    assert kw["embed"].title == "🐾 Hi" and kw["embed"].image.url == PIC                 # the old embed is kept
    assert type(kw["view"]) is discord.ui.View and kw["view"].children[0].item.label.startswith("Ran away")


def test_adopt_on_a_v2_spawn_edits_it_as_v2(monkeypatch):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("ok", mkcat(rarity="rare"))))
    i = FI(10, _v2_message(v.spawn_post(5, "A cat", PIC, NEXT)))
    run(v.AdoptButton(5).callback(i))
    e = i.edits[0]
    assert "embed" not in e and "adopted by <@10>" in v.view_text(e["view"]) and "Rare" in v.view_text(e["view"])
    assert "adopted **Mochi**" in v.view_text(i.followups[0][1]["view"])        # the private follow-up is unchanged


def test_adopt_on_an_old_classic_spawn_still_edits_the_embed(monkeypatch):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("ok", mkcat(rarity="rare"))))
    i = FI(10, _classic_message())
    run(v.AdoptButton(5).callback(i))
    e = i.edits[0]
    assert "adopted by <@10>" in e["embed"].description and type(e["view"]) is discord.ui.View
    assert e["embed"].title == "🐾 Hi"


@pytest.mark.parametrize("make", [lambda: _v2_message(v.spawn_post(5, "A cat", PIC)), _classic_message])
def test_an_expired_spawn_is_closed_for_everybody_on_either_kind_of_post(monkeypatch, make):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("expired")))
    msg = make()
    i = FI(10, msg)
    run(v.AdoptButton(5).callback(i))
    kw = msg.edit.await_args.kwargs
    if "embed" in kw:
        assert kw["embed"].description == ca.EXPIRED_NOTE
    else:
        assert ca.EXPIRED_NOTE in v.view_text(kw["view"]) and "view" in kw


@pytest.mark.parametrize("make,key", [(lambda: _v2_message(v.spawn_post(1, "A cat", PIC)), "view"),
                                      (_classic_message, "embed")])
def test_fade_spawns_closes_either_kind_of_post(monkeypatch, make, key):
    rows = [dict(id=1, guild_id=7, channel_id=2, message_id=30, pool_id=9)]
    monkeypatch.setattr(ca, "expire_spawns", AsyncMock(return_value=rows))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    msg = make()
    ch = SimpleNamespace(fetch_message=AsyncMock(return_value=msg))
    c = cat.CatCog.__new__(cat.CatCog)
    c.bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_channel=lambda cid: ch))
    assert run(c.fade_spawns(None)) == 1
    kw = msg.edit.await_args.kwargs
    assert key in kw and (("embed" in kw) == (key == "embed"))
    if key == "view":
        assert ca.EXPIRED_NOTE in v.view_text(kw["view"])


def test_post_cat_sends_a_v2_card_with_the_five_buttons(monkeypatch):
    from tests.unit.test_cat_adopt import _guild_with_channel
    g, ch = _guild_with_channel()
    monkeypatch.setattr(cat, "fetch_cat_url", AsyncMock(return_value=cat_pool.PooledUrl(PIC, 9)))
    monkeypatch.setattr(ca, "create_spawn", AsyncMock(return_value=31))
    monkeypatch.setattr(ca, "set_spawn_message", AsyncMock())
    assert run(cat.post_cat(SimpleNamespace(clone_id=None), g, 1, NEXT)) is True
    kw = ch.send.await_args.kwargs
    assert isinstance(kw["view"], discord.ui.LayoutView) and "embed" not in kw and "content" not in kw
    assert kw["allowed_mentions"].users is False
    assert "Next cat" in v.view_text(kw["view"]) and _count(kw["view"]) <= 40


def test_hub_share_this_cat_posts_a_v2_card_without_buttons():
    sent = []
    channel = SimpleNamespace(send=AsyncMock(side_effect=lambda **k: sent.append(k)))
    i = SimpleNamespace(channel=channel, response=SimpleNamespace(send_message=AsyncMock()))
    btn = hub._ShareButton(SimpleNamespace(picture_url=PIC))
    run(btn.callback(i))
    assert isinstance(sent[0]["view"], discord.ui.LayoutView) and "embed" not in sent[0]
    assert _ids(sent[0]["view"]) == [] and _count(sent[0]["view"]) == 3


# ── for-sale post ──────────────────────────────────────────────────────────────────────────────────────────────────

def _listing(status="open"):
    return cm.Listing(id=9, adoption_id=1, seller_id=20, seller_clone_id=None, price=500, guild_id=5, channel_id=2,
                      message_id=60, status=status, cat=mkcat(1, 20, "rare"))


def test_listing_card_budget_and_buttons():
    open_card = run(vm.listing_layout(None, _listing(), open_=True))
    assert 6 <= _count(open_card) <= 40 and _ids(open_card) == ["cat_buy:9", "cat_unlist:9"]
    text = v.view_text(open_card)
    assert "Mochi is for sale" in text and "500 coins" in text and "<@20>" in text and vm.NOTE in text
    closed = run(vm.listing_layout(None, _listing(), "✅ **Sold**"))
    assert _ids(closed) == [] and v.view_text(closed).splitlines()[1] == "✅ **Sold**"


def test_listing_card_without_a_picture_still_builds():
    cat_pool.url_for = AsyncMock(return_value=None)
    view = run(vm.listing_layout(None, _listing(), open_=True))
    assert not any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())


def test_publish_listing_posts_one_v2_message_and_records_it(monkeypatch):
    monkeypatch.setattr(cm, "create_listing", AsyncMock(return_value=("ok", _listing())))
    setmsg = AsyncMock()
    monkeypatch.setattr(cm, "set_message", setmsg)
    ch = SimpleNamespace(id=2, send=AsyncMock(return_value=SimpleNamespace(id=60)))
    i = SimpleNamespace(channel=ch, guild=SimpleNamespace(id=5), user=SimpleNamespace(id=20),
                        client=SimpleNamespace(clone_id=None))
    out = run(vm.publish_listing(i, mkcat(1, 20, "rare"), 500))
    kw = ch.send.await_args.kwargs
    assert isinstance(kw["view"], discord.ui.LayoutView) and "embed" not in kw and kw["allowed_mentions"].users is False
    assert _ids(kw["view"]) == ["cat_buy:9", "cat_unlist:9"]
    setmsg.assert_awaited_once_with(9, 2, 60)
    assert "is for sale for **500 coins**" in out


def test_publish_listing_undoes_the_listing_if_the_post_fails(monkeypatch):
    monkeypatch.setattr(cm, "create_listing", AsyncMock(return_value=("ok", _listing())))
    cancel = AsyncMock()
    monkeypatch.setattr(cm, "cancel", cancel)
    boom = discord.HTTPException(SimpleNamespace(status=403, reason="no"), "x")
    ch = SimpleNamespace(id=2, send=AsyncMock(side_effect=boom))
    i = SimpleNamespace(channel=ch, guild=SimpleNamespace(id=5), user=SimpleNamespace(id=20),
                        client=SimpleNamespace(clone_id=None))
    assert "couldn't post" in run(vm.publish_listing(i, mkcat(1, 20), 500))
    cancel.assert_awaited_once()


@pytest.mark.parametrize("v2", [True, False])
def test_buying_closes_either_kind_of_listing_post(monkeypatch, v2):
    sold = _listing("sold")
    monkeypatch.setattr(cm, "buy", AsyncMock(return_value=cm.BuyResult("ok", sold)))
    monkeypatch.setattr(vm.cat_log, "record", lambda *a, **k: None)
    monkeypatch.setattr(vm, "_after_sale", AsyncMock())
    msg = _v2_message(run(vm.listing_layout(None, _listing(), open_=True))) if v2 else _classic_message()
    i = FI(30, msg)
    i.guild = SimpleNamespace(id=5, name="S")
    run(vm.BuyButton(9).callback(i))
    kw = i.edits[0]
    if v2:
        assert "embed" not in kw and _ids(kw["view"]) == [] and "Sold" in v.view_text(kw["view"])
        assert kw["allowed_mentions"].users is False
    else:
        assert "Sold" in kw["embed"].description and kw["view"] is None


@pytest.mark.parametrize("v2", [True, False])
def test_taking_a_cat_back_closes_either_kind_of_listing_post(monkeypatch, v2):
    monkeypatch.setattr(cm, "cancel", AsyncMock(return_value=True))
    monkeypatch.setattr(cm, "get", AsyncMock(return_value=_listing("cancelled")))
    msg = _v2_message(run(vm.listing_layout(None, _listing(), open_=True))) if v2 else _classic_message()
    i = FI(20, msg)
    run(vm.UnlistButton(9).callback(i))
    kw = i.edits[0]
    if v2:
        assert "embed" not in kw and "took this cat off the market" in v.view_text(kw["view"]) and _ids(kw["view"]) == []
    else:
        assert "took this cat off" in kw["embed"].description and kw["view"] is None


class _Part:
    def __init__(self, fail_classic=False, fail_v2=False):
        self.calls = []
        self.fail_classic, self.fail_v2 = fail_classic, fail_v2

    async def edit(self, **kw):
        self.calls.append(kw)
        bad = (self.fail_classic and "embed" in kw) or (self.fail_v2 and "embed" not in kw)
        if bad:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="bad"), "wrong kind")


def _client(part):
    return SimpleNamespace(get_partial_messageable=lambda cid: SimpleNamespace(get_partial_message=lambda mid: part))


def test_closing_a_post_from_the_browser_tries_classic_first_then_v2():
    classic = _Part()                                      # an older post: the first (classic) edit works
    run(vm.close_listing_post(_client(classic), _listing(), "sold"))
    assert len(classic.calls) == 1 and "embed" in classic.calls[0] and classic.calls[0]["view"] is None
    new = _Part(fail_classic=True)                         # a V2 post refuses the embed edit, so the V2 edit follows
    run(vm.close_listing_post(_client(new), _listing(), "sold"))
    assert len(new.calls) == 2 and "embed" not in new.calls[1] and _ids(new.calls[1]["view"]) == []
    assert new.calls[1]["allowed_mentions"].users is False


def test_closing_a_post_never_raises_and_skips_listings_without_a_message():
    both_fail = _Part(fail_classic=True, fail_v2=True)
    run(vm.close_listing_post(_client(both_fail), _listing(), "sold"))              # must not raise
    nomsg = _Part()
    run(vm.close_listing_post(_client(nomsg), cm.Listing(1, 1, 20, None, 5, 5, None, None, "sold", None), "sold"))
    assert nomsg.calls == []


# ── announcements ──────────────────────────────────────────────────────────────────────────────────────────────────

class _Inter:
    def __init__(self, guild=True, followup_fails=False):
        self.sent = []
        self.guild = SimpleNamespace(id=1) if guild else None
        self.client = SimpleNamespace(clone_id=None)
        self.user = SimpleNamespace(id=7, mention="<@7>")
        self.channel = SimpleNamespace(id=2, send=AsyncMock(side_effect=lambda **k: self.sent.append(("channel", k))))
        self._fail = followup_fails
        self.followup = SimpleNamespace(send=self._fsend)

    async def _fsend(self, **k):
        if self._fail:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        self.sent.append(("followup", k))


@pytest.fixture
def all_mode(monkeypatch):
    monkeypatch.setattr(cat_announce, "mode_for", AsyncMock(return_value="all"))


def test_announcement_card_budget_and_colours(all_mode):
    plain = cat_announce.card_view("hi")
    assert _count(plain) == 2 <= 40
    gold = cat_announce.card_view("hi", gold=True, image="attachment://x.png", footer="small")
    assert _count(gold) == 4
    assert [int(getattr(c.accent_colour, "value", c.accent_colour)) for c in plain.children] == [cat_announce.PINK]
    assert [int(getattr(c.accent_colour, "value", c.accent_colour)) for c in gold.children] == [cat_announce.GOLD]


@pytest.mark.parametrize("fail,where", [(False, "followup"), (True, "channel")])
def test_announcements_go_out_as_v2_in_both_routes_and_never_ping(all_mode, fail, where):
    i = _Inter(followup_fails=fail)
    assert run(cat_announce.announce(i, "🐾 <@7> did it", gold=True)) is True
    kind, kw = i.sent[0]
    assert kind == where and isinstance(kw["view"], discord.ui.LayoutView) and "embed" not in kw and "content" not in kw
    assert kw["allowed_mentions"].users is False and kw["allowed_mentions"].roles is False
    assert "<@7>" in v.view_text(kw["view"])


def test_announcements_still_respect_dms_and_the_server_setting(monkeypatch):
    assert run(cat_announce.announce(_Inter(guild=False), "x")) is False
    monkeypatch.setattr(cat_announce, "mode_for", AsyncMock(return_value="off"))
    assert run(cat_announce.announce(_Inter(), "x")) is False


# ── shared cards ───────────────────────────────────────────────────────────────────────────────────────────────────

def _profile(show=True):
    return {"bio": "I like cats", "collection_size": 4, "rarest": mkcat(1, 7, "rare"), "pinned": [mkcat(2, 7)],
            "equipped": {"hat": {"emoji": "👑", "name": "Crown"}}, "show_profile": show}


def test_public_profile_card_budget_and_privacy():
    view = cat_share.profile_view("Max", _profile(), {"legend"}, "https://cdn/a.png")
    assert 3 <= _count(view) <= 40
    text = v.view_text(view)
    assert "Max's cat profile" in text and "I like cats" in text and "Mochi" in text and "Legend Keeper" in text
    assert "coin" not in text.lower() and "diamond" not in text.lower()
    plain = cat_share.profile_view("Max", _profile(), set())
    assert _count(plain) == 2                                         # container + text, no avatar


def test_badge_card_is_none_without_badges():
    assert cat_share.badges_view("Max", set()) is None
    assert "Full Set" in v.view_text(cat_share.badges_view("Max", {"full-set"}))


def test_profile_screen_budget_owner_lock_and_card_swap(monkeypatch):
    view = vf.ProfileView(_profile(), "Max", {"legend"}, "https://cdn/a.png", footer=vf.HIDDEN_NOTE)
    assert 8 <= _count(view) <= 40
    labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert labels == ["Share", "Edit bio", "Pin cats", "Clear pins", "Hide profile"]
    assert "-# Hidden: only you can see this." in v.view_text(view)
    vis.lock_to_owner(view, 7)
    owner_lock = view.interaction_check
    view.show_card()
    assert view.interaction_check is owner_lock                       # same view object, so the lock is kept
    assert any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())
    assert [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)] == labels
    assert _count(view) <= 40


def test_a_hidden_profile_disables_share():
    view = vf.ProfileView(_profile(show=False), "Max", set(), None)
    share = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)][0]
    assert share.disabled and [c.label for c in view.walk_children()
                               if isinstance(c, discord.ui.Button)][-1] == "Show profile"


def test_the_drawn_card_is_swapped_into_the_same_private_screen(monkeypatch):
    from modules import cat_profile_card as pc
    monkeypatch.setattr(pc, "render", AsyncMock(return_value=b"\x89PNG"))
    edits = []

    class I:
        client = SimpleNamespace(clone_id=None)

        async def edit_original_response(self, **k):
            edits.append(k)
    view = vf.ProfileView(_profile(), "Max", set(), None)
    run(vf.upgrade_to_card(I(), SimpleNamespace(display_name="Max"), _profile(), set(), view=view))
    assert edits[0]["view"] is view and edits[0]["attachments"][0].filename == pc.FILE_NAME and "embed" not in edits[0]
    assert any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())


def test_nothing_in_the_public_paths_passes_an_embed_any_more():
    import inspect
    for fn in (cat_announce.announce, cat_announce.announce_card, vm.publish_listing, hub._ShareButton.callback,
               vf.show_public_profile, vf.upgrade_to_card, vf.ProfileButton.callback, cat.CatCog.fade_spawns,
               cat.post_cat):
        assert "embed=" not in inspect.getsource(fn), fn.__name__


# ── discord.py's own dispatch path, end to end, on a V2 message ───────────────────────────────────────────────────

def _dispatch(view_classes, message, custom_id, interaction_extra=None):
    """Runs a real ViewStore.dispatch_dynamic_items for a button on `message` (no network)."""
    from discord.ui.view import ViewStore

    async def go():
        store = ViewStore(SimpleNamespace())
        store.add_dynamic_items(*view_classes)
        i = SimpleNamespace(message=message, data={"custom_id": custom_id, "component_type": 2}, **(interaction_extra or {}))
        store.dispatch_dynamic_items(2, custom_id, i)
        await asyncio.gather(*[t for t in list(store._tasks)] if hasattr(store, "_tasks") else [])
        await asyncio.sleep(0.05)
    run(go())


def test_discord_py_finds_and_runs_the_adopt_button_inside_a_v2_container(monkeypatch):
    calls = []

    async def fake_callback(self, interaction):
        calls.append(("adopt", self.spawn_id))
    monkeypatch.setattr(v.AdoptButton, "callback", fake_callback)
    msg = _v2_message(v.spawn_post(31, "A cat", PIC, NEXT))
    _dispatch(v.DYNAMIC_ITEMS, msg, "cat_adopt:31")
    assert calls == [("adopt", 31)]


def test_discord_py_finds_and_runs_the_buy_and_take_back_buttons_inside_a_v2_container(monkeypatch):
    calls = []

    async def buy(self, interaction):
        calls.append(("buy", self.listing_id))

    async def unlist(self, interaction):
        calls.append(("unlist", self.listing_id))
    monkeypatch.setattr(vm.BuyButton, "callback", buy)
    monkeypatch.setattr(vm.UnlistButton, "callback", unlist)
    msg = _v2_message(run(vm.listing_layout(None, _listing(), open_=True)))
    _dispatch([vm.BuyButton, vm.UnlistButton], msg, "cat_buy:9")
    _dispatch([vm.BuyButton, vm.UnlistButton], msg, "cat_unlist:9")
    assert calls == [("buy", 9), ("unlist", 9)]


def test_the_same_dispatch_still_works_on_an_old_classic_message(monkeypatch):
    calls = []

    async def fake_callback(self, interaction):
        calls.append(self.spawn_id)
    monkeypatch.setattr(v.AdoptButton, "callback", fake_callback)
    flags = discord.MessageFlags()
    classic_view = v.spawn_view(31)
    payload = classic_view.to_components()
    msg = SimpleNamespace(flags=flags, embeds=[], components=[discord.components._component_factory(c) for c in payload])
    _dispatch(v.DYNAMIC_ITEMS, msg, "cat_adopt:31")
    assert calls == [31]
