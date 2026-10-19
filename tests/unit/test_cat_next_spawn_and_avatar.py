import discord
"""Next-cat time on spawns, avatar on the cat profile, and the fuller help text."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from discord_bot.cogs import cat
from modules import cat_help, cat_setup, cat_share


def _cfg(start=0, end=24):
    return cat_setup.CatConfig(guild_id=1, active_start=start, active_end=end)


def test_spawn_embed_shows_next_cat_time():
    when = datetime(2026, 10, 11, 20, 40, tzinfo=timezone.utc)
    e = cat.cat_embed("https://x/y.jpg", when)
    ts = int(when.timestamp())
    f = [f for f in e.fields if "Next cat" in f.name]
    assert f and f"<t:{ts}:t>" in f[0].value and f"<t:{ts}:R>" in f[0].value


def test_spawn_embed_without_next_time_is_unchanged():
    assert not cat.cat_embed("https://x/y.jpg").fields


def test_upcoming_is_the_scheduled_time_when_open_all_day():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    nxt = now + timedelta(minutes=11)
    assert cat_setup.upcoming(_cfg(), nxt, now) == nxt


def test_upcoming_waits_for_the_window_when_next_falls_outside_it():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    got = cat_setup.upcoming(_cfg(8, 14), now + timedelta(hours=3), now)       # 15:00 is closed
    assert got == datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def test_upcoming_none_and_naive_times():
    assert cat_setup.upcoming(_cfg(), None) is None
    naive = datetime.utcnow() + timedelta(minutes=5)
    assert cat_setup.upcoming(_cfg(), naive).tzinfo is not None


def test_profile_card_has_avatar_only_when_given():
    profile = {"bio": "", "collection_size": 2, "rarest": None, "pinned": [], "show_profile": True}
    def thumbs(view):
        return [t.media.url for t in view.walk_children() if isinstance(t, discord.ui.Thumbnail)]
    assert thumbs(cat_share.profile_view("Ama", profile, set(), "https://cdn/a.png")) == ["https://cdn/a.png"]
    assert thumbs(cat_share.profile_view("Ama", profile, set())) == []


def test_avatar_helper_never_raises():
    from discord_bot.cogs._views_cat_features import _avatar
    assert _avatar(SimpleNamespace(display_avatar=SimpleNamespace(url="u"))) == "u"
    assert _avatar(object()) is None


def test_help_covers_spawn_time_profile_leaderboard_and_owner_setup():
    text = cat_help.build_text()
    for needle in ("Next cat", "/catsetup", "profile:@player", "Leaderboard", "Discord picture"):
        assert needle in text
    assert all(len(b) <= 1024 for _, b in cat_help.sections())
