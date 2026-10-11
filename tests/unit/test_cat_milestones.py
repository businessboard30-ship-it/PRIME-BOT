"""Milestone posts + the per-server public-post setting: posted on success, not on failure / wrong mode / rate limit, no pings."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from modules import cat_announce as an
from modules import cat_setup as cs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Inter:
    def __init__(self, uid=7, gid=1, channel=100, fails=False, guild=True):
        self.guild = SimpleNamespace(id=gid) if guild else None
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>")
        self.client = SimpleNamespace(clone_id=None)
        self.channel = SimpleNamespace(id=channel, send=self._chsend)
        self.followup = self
        self.sent, self._fails = [], fails

    async def send(self, **kw):
        if self._fails:
            raise RuntimeError("boom")
        self.sent.append(kw)

    async def _chsend(self, **kw):
        raise RuntimeError("no perms")


def setmode(mode, gid=1):
    an._mode_cache.clear(); an._user_posts.clear(); an._channel_last.clear()
    an.remember_mode(gid, None, mode)


def test_modes_gate_event_and_milestone_posts():
    for mode, event_ok, ms_ok in (("all", True, True), ("milestones", False, True), ("off", False, False)):
        setmode(mode)
        e, m = Inter(), Inter(channel=101)
        assert run(an.announce(e, "x")) is event_ok and bool(e.sent) is event_ok
        assert run(an.announce(m, "x", kind="milestone")) is ms_ok and bool(m.sent) is ms_ok


def test_milestone_never_pings_and_is_public():
    setmode("all")
    i = Inter()
    assert run(an.announce(i, "x", kind="milestone"))
    kw = i.sent[0]
    assert kw["ephemeral"] is False and kw["allowed_mentions"].users is False and kw["allowed_mentions"].roles is False


def test_milestone_rate_limits_per_user_and_channel():
    setmode("all")
    assert run(an.announce(Inter(), "x", kind="milestone"))
    assert not run(an.announce(Inter(uid=8), "x", kind="milestone"))               # same channel, within 5s
    t = 1000.0
    an._channel_last.clear(); an._user_posts.clear()
    for n in range(an.USER_LIMIT):
        assert an.milestone_ok(7, 5, now=t + n * 10)
        an._mark_milestone(7, 5, now=t + n * 10)
    assert not an.milestone_ok(7, 6, now=t + 100)                                  # 4th within the hour
    assert an.milestone_ok(7, 6, now=t + an.USER_WINDOW + 50)                      # window passed
    assert an.milestone_ok(9, 6, now=t + 100)                                      # other players unaffected


def test_failed_post_is_not_counted_and_never_raises():
    setmode("all")
    f = Inter(fails=True)
    assert run(an.announce(f, "x", kind="milestone")) is False
    assert run(an.announce(Inter(), "x", kind="milestone")) is True               # failure didn't use up the limit
    assert run(an.announce(Inter(guild=False), "x", kind="milestone")) is False   # DMs


def test_only_rare_badges_are_announced():
    setmode("all")
    i = Inter()
    run(an.announce_badges(i, ["rare-hunter", "coat-collector"]))
    assert not i.sent
    run(an.announce_badges(i, ["full-set"]))
    assert len(i.sent) == 1 and "Full Set" in i.sent[0]["embed"].description and "<@7>" in i.sent[0]["embed"].description
    setmode("off")
    j = Inter()
    run(an.announce_badges(j, ["legend"]))
    assert not j.sent


def test_texts_have_no_private_numbers():
    for t in (an.streak_text("<@1>", 7), an.quests_text("<@1>"), an.badge_text("<@1>", "legend")):
        assert "coin" not in t.lower() and "diamond" not in t.lower() and "💎" not in t
    assert an.MILESTONE_STREAKS == (7, 30)


def test_setting_defaults_and_clamps():
    assert cs.CatConfig(guild_id=1).announce_mode == "all"
    assert cs.clamp_announce("off") == "off" and cs.clamp_announce("weird") == "all" and cs.clamp_announce(None) == "all"
    row = {"guild_id": 1, "clone_id": None, "enabled": True, "channel_ids": [], "interval_minutes": 1, "next_at": None,
           "last_post_at": None}
    class R(dict): pass
    assert cs.CatConfig.from_row(R(row)).announce_mode == "all"                    # older rows without the column
    assert cs.CatConfig.from_row(R(row | {"announce_mode": "milestones"})).announce_mode == "milestones"


def test_wired_in():
    feats = (ROOT / "discord_bot/cogs/_views_cat_features.py").read_text(encoding="utf-8")
    adopt = (ROOT / "discord_bot/cogs/_views_cat_adopt.py").read_text(encoding="utf-8")
    setup = (ROOT / "discord_bot/cogs/_views_cat_setup.py").read_text(encoding="utf-8")
    assert "MILESTONE_STREAKS" in feats and "announce_badges(interaction, new)" in feats and "quests_text(" in feats
    assert "announce_badges(interaction, earned)" in adopt
    assert "_on_announce" in setup and "announce_mode" in setup
    assert (ROOT / "database/migrations/046_cat_announce_mode.sql").exists()
    assert "046_cat_announce_mode.sql" in (ROOT / "database.py").read_text(encoding="utf-8")
