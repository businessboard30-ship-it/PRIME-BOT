"""Share buttons and the public cat profile: consent based, private data stays private, never pings (no database)."""
import asyncio
from discord_bot.cogs._views_cat_adopt import view_text
from pathlib import Path
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_cat_features as v
from modules import cat_adopt, cat_features, cat_quests, cat_share

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Resp:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def send_message(self, *a, **kw):
        self.sent.append(kw | {"args": a})

    async def defer(self, **kw):
        self.deferred = True


class Inter:
    def __init__(self, uid=7, guild=True, followup_fails=False, bot=False):
        self.user = SimpleNamespace(id=uid, display_name="Maxwell", mention=f"<@{uid}>", bot=bot)
        self.guild = object() if guild else None
        self.client = SimpleNamespace(clone_id=None)
        self.response, self.public, self.private = Resp(), [], []
        self.followup = self
        self.channel = None
        self._ff = followup_fails

    async def send(self, *a, **kw):
        if kw.get("ephemeral"):
            self.private.append(kw | {"args": a})
            return
        if self._ff:
            raise RuntimeError("boom")
        self.public.append(kw)


def cat(i, name="Mochi", rarity="legendary", trait="Royal"):
    return cat_adopt.Cat(id=i, owner_id=7, clone_id=None, pool_id=1, name=name, rarity=rarity, trait=trait)


def profile(show=True, size=3, bio="hi", pinned=()):
    return {"owner_id": 7, "bio": bio, "show_profile": show, "collection_size": size,
            "rarest": cat(1) if size else None, "pinned": list(pinned)}


def fake(monkeypatch, prof, badges=frozenset()):
    async def pp(*a):
        return prof

    async def ob(*a):
        return set(badges)
    monkeypatch.setattr(cat_features, "public_profile", pp)
    monkeypatch.setattr(cat_quests, "owned_badges", ob)
    cat_share._last_share.clear()


def press(kind, inter, streak=0):
    async def go():
        await v.ShareButton(kind, streak=streak).callback(inter)
    run(go())


def test_clean_bio_plain_text_only():
    assert cat_share.clean_bio("I love cats <@123> @everyone") == "I love cats 123 everyone"
    assert cat_share.clean_bio("join discord.gg/abc") is None
    assert cat_share.clean_bio("see https://x.io") is None
    assert cat_share.clean_bio(None) == ""
    assert len(cat_share.clean_bio("a" * 999)) == cat_share.BIO_MAX


def test_card_has_no_private_data_and_only_pinned_cats():
    view = cat_share.profile_view("Max", profile(pinned=[cat(2, "Tofu", "rare", "Cuddly")]), {"full-set", "legend"})
    text = view_text(view).lower()
    assert "tofu" in text and "legend keeper" in text and "full set" in text
    assert "coin" not in text and "diamond" not in text and "wallet" not in text


def test_share_profile_posts_publicly_without_pings(monkeypatch):
    fake(monkeypatch, profile(), {"legend"})
    i = Inter()
    press("profile", i)
    assert i.response.deferred and len(i.public) == 1 and not i.private
    kw = i.public[0]
    assert kw["ephemeral"] is False and kw["allowed_mentions"].users is False and kw["allowed_mentions"].roles is False


def test_hidden_profile_is_never_shared(monkeypatch):
    fake(monkeypatch, profile(show=False))
    i = Inter()
    press("profile", i)
    assert not i.public and i.private and "hidden" in i.private[0]["args"][0]


def test_nothing_posted_on_failure_dm_or_no_badges(monkeypatch):
    fake(monkeypatch, profile())
    i = Inter(followup_fails=True)
    press("profile", i)
    assert not i.public and i.private                      # the post failed: nothing public, told privately, no crash
    d = Inter(guild=False)
    press("profile", d)
    assert not d.public and d.response.sent and not d.response.deferred   # DMs: told privately
    fake(monkeypatch, profile(), set())
    b = Inter()
    press("badges", b)
    assert not b.public and b.private                      # no badges: nothing to brag about


def test_share_is_rate_limited_per_player_and_kind(monkeypatch):
    fake(monkeypatch, profile())
    i = Inter()
    press("profile", i)
    press("profile", i)
    assert len(i.public) == 1 and i.response.sent          # second tap refused privately
    press("care", i, streak=5)
    assert len(i.public) == 2                              # another kind is fine


def test_care_share_shows_streak_but_no_rewards(monkeypatch):
    fake(monkeypatch, profile())
    i = Inter()
    press("care", i, streak=7)
    d = view_text(i.public[0]["view"])
    assert "7-day" in d and "coin" not in d.lower() and "diamond" not in d.lower()


def test_public_profile_command(monkeypatch):
    fake(monkeypatch, profile(), {"legend"})
    i = Inter(uid=1)
    run(v.show_public_profile(i, SimpleNamespace(id=7, display_name="Max", bot=False)))
    kw = i.response.sent[0]
    assert kw["ephemeral"] is False and kw["allowed_mentions"].users is False and "embed" not in kw
    assert isinstance(kw["view"], discord.ui.LayoutView) and "Max's cat profile" in view_text(kw["view"])


def test_public_profile_respects_hidden_and_bots(monkeypatch):
    fake(monkeypatch, profile(show=False))
    i = Inter(uid=1)
    run(v.show_public_profile(i, SimpleNamespace(id=7, display_name="Max", bot=False)))
    assert i.response.sent[0]["ephemeral"] is True and "embed" not in i.response.sent[0]
    b = Inter(uid=1)
    run(v.show_public_profile(b, SimpleNamespace(id=9, display_name="Bot", bot=True)))
    assert b.response.sent[0]["ephemeral"] is True


def test_own_hidden_profile_preview_stays_private(monkeypatch):
    fake(monkeypatch, profile(show=False))
    i = Inter(uid=7)
    run(v.show_public_profile(i, SimpleNamespace(id=7, display_name="Max", bot=False)))
    assert i.response.sent[0]["ephemeral"] is True and "view" in i.response.sent[0] and "embed" not in i.response.sent[0]


def test_wired_in():
    cat_src = (ROOT / "discord_bot/cogs/cat.py").read_text(encoding="utf-8")
    views = (ROOT / "discord_bot/cogs/_views_cat_features.py").read_text(encoding="utf-8")
    assert "profile: Optional[discord.User]" in cat_src and "show_public_profile(" in cat_src
    assert 'ShareButton("care"' in views and 'ShareButton("badges")' in views and "ProfileView(profile," in views
    assert "cat_features.save_profile(" in views            # the bio / pin UI finally calls it
