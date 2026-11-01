"""AI scam lookup: admin-only, public, evidence built from recorded data, defanged, no cross-server leaks."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from modules import ai_scam_lookup as sl

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
UID = 123456789012345678


def run(c):
    return asyncio.run(c)


def data(**kw):
    d = {"here_total": 0, "hits": [], "other": None, "warn_total": 0, "warns": [], "errors": []}
    d.update(kw)
    return d


def hit(i=1, **kw):
    h = {"id": i, "channel_id": 55, "kind": "domain", "matched": "fatowin.com", "snippet": "free nitro https://fatowin.com/x @everyone",
         "deleted": True, "created_at": NOW - timedelta(days=1), "note": "MrBeast casino scam"}
    h.update(kw)
    return h


def test_parse_user_id():
    assert sl.parse_user_id("123456789012345678") == UID
    assert sl.parse_user_id(" <@123456789012345678> ") == UID and sl.parse_user_id("<@!123456789012345678>") == UID
    for bad in ("abc", "123", "", None, "12345678901234567890123", "1234567890123456 7", "@everyone"):
        assert sl.parse_user_id(bad) is None


def test_defang_removes_links_pings_and_markdown():
    t = sl.defang("claim at https://evil.example.com/win now @everyone <@123> `x` **b**")
    assert "http://" not in t and "https://" not in t and "hxxps://" in t and "[.]" in t
    assert "@\u200beveryone" in t and "<@" not in t and "`" not in t and "\\*\\*b" in t
    assert len(sl.defang("a" * 500)) <= sl.SNIPPET_CHARS + 1


def test_assessment_is_deterministic_from_recorded_counts():
    assert sl.assess(data())[0] == "none"
    assert sl.assess(data(here_total=1))[0] == "some"
    assert sl.assess(data(other={"catches": 1}))[0] == "some"
    assert sl.assess(data(here_total=2))[0] == "strong"
    assert sl.assess(data(here_total=1, other={"catches": 1}))[0] == "strong"


def _text(e):
    return "\n".join([e.title or "", e.description or ""] + [f.name + "\n" + f.value for f in e.fields] + [e.footer.text or ""])


def test_embed_with_evidence_is_defanged_and_cites_each_catch():
    e = sl.build_embed(UID, data(here_total=2, hits=[hit(7), hit(3, snippet=None, kind="image", matched=None, note=None, deleted=False)]),
                       {"in_server": True, "joined_at": NOW - timedelta(days=2), "display": "Scammy", "banned": None})
    t = _text(e)
    assert "Strong evidence" in t and "**#7**" in t and "**#3**" in t
    assert "hxxps://" in t and "https://" not in t and "(text no longer stored)" in t
    assert "message deleted" in t and "message kept" in t and "MrBeast casino scam" in t
    assert "@\u200beveryone" in t and "not a verdict" in t.lower()
    assert "<#55>" in t and "<t:" in t


def test_embed_with_nothing_says_so_plainly():
    e = sl.build_embed(UID, data(), {"in_server": False, "banned": False})
    t = _text(e)
    assert "No recorded evidence" in t and "Not currently in this server" in t and "None recorded." in t


def test_other_servers_are_counts_only_never_names_or_text():
    other = {"catches": 3, "servers": 2, "last_at": NOW, "kinds": ["a known scam link"], "guild_name": "SECRET SERVER", "snippet": "SECRET TEXT"}
    t = _text(sl.build_embed(UID, data(other=other), {"in_server": False}))
    assert "3 catch(es) across 2 server(s)" in t and "SECRET" not in t


def test_banned_and_errors_are_reported():
    e = sl.build_embed(UID, data(errors=["warns"]), {"in_server": False, "banned": True, "timed_out": False})
    t = _text(e)
    assert "Banned here" in t and "Couldn't read: warns" in t


def test_embed_fits_discord_limits_with_worst_case_input():
    hits = [hit(i, snippet="x" * 1000, matched="m" * 200, note="n" * 500) for i in range(sl.MAX_HITS_SHOWN)]
    e = sl.build_embed(UID, data(here_total=99, hits=hits, warn_total=5, warns=[{"id": 1, "reason": "r" * 900, "created_at": NOW}]),
                       {"in_server": True, "display": "d" * 300})
    assert all(len(f.value) <= 1024 for f in e.fields) and len(e) <= 6000


def test_cooldown_per_person():
    sl._last.clear()
    assert sl.on_cooldown(1, now=10.0) == 0
    assert sl.on_cooldown(1, now=12.0) > 0
    assert sl.on_cooldown(2, now=12.0) == 0
    assert sl.on_cooldown(1, now=10.0 + sl.COOLDOWN_SECONDS + 1) == 0


def test_tool_schema_and_not_a_slash_command():
    t = sl.tool_schema()
    assert t["function"]["name"] == "scam_lookup" and t["function"]["parameters"]["required"] == ["user_id"]
    assert "ADMIN ONLY" in t["function"]["description"]
    from modules.ai_command_allowlist import AI_COMMANDS
    assert not [c for c in AI_COMMANDS if "scam_lookup" in c.name]
    assert "scam_lookup" not in (ROOT / "modules/ai_command_guard.py").read_text()


# ---- the handler itself, with fakes ------------------------------------------------------------------------------

class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


def make_ctx(is_admin):
    perms = SimpleNamespace(administrator=is_admin, manage_guild=True)
    guild = SimpleNamespace(id=9, get_member=lambda i: None,
                            fetch_ban=lambda o: (_ for _ in ()).throw(discord.NotFound(SimpleNamespace(status=404, reason=""), "x")))

    async def fetch_ban(o):
        raise discord.NotFound(SimpleNamespace(status=404, reason=""), "unknown ban")
    guild.fetch_ban = fetch_ban
    return SimpleNamespace(guild=guild, user=SimpleNamespace(id=77), permissions=perms, followup=Followup())


@pytest.fixture
def cog(monkeypatch):
    from discord_bot.cogs.ai_tools import AIToolsCog
    called = []

    async def fake_gather(g, u):
        called.append((g, u))
        return data(here_total=1, hits=[hit()])
    monkeypatch.setattr(sl, "gather", fake_gather)
    sl._last.clear()
    c = AIToolsCog.__new__(AIToolsCog)
    c.called = called
    return c


def test_non_admin_is_refused_privately_and_nothing_is_queried(cog):
    ctx = make_ctx(False)
    run(cog._handle_scam_lookup(ctx, {"user_id": str(UID)}))
    (msg, kw), = ctx.followup.sent
    assert "Only server admins" in msg and kw.get("ephemeral") is True and not cog.called


def test_admin_gets_a_public_embed(cog):
    ctx = make_ctx(True)
    run(cog._handle_scam_lookup(ctx, {"user_id": str(UID)}))
    (content, kw), = ctx.followup.sent
    assert kw["ephemeral"] is False and isinstance(kw["embed"], discord.Embed) and cog.called == [(9, UID)]
    assert "Some evidence" in kw["embed"].title


def test_bad_id_and_dm_and_cooldown(cog):
    ctx = make_ctx(True)
    run(cog._handle_scam_lookup(ctx, {"user_id": "not an id"}))
    assert "Discord ID" in ctx.followup.sent[0][0] and not cog.called
    run(cog._handle_scam_lookup(SimpleNamespace(guild=None, user=SimpleNamespace(id=1), permissions=SimpleNamespace(administrator=True),
                                                followup=Followup()), {"user_id": str(UID)}))
    ctx2 = make_ctx(True)
    run(cog._handle_scam_lookup(ctx2, {"user_id": str(UID)}))
    run(cog._handle_scam_lookup(ctx2, {"user_id": str(UID)}))
    assert "Please wait" in ctx2.followup.sent[1][0] and len(cog.called) == 1


def test_wiring_only_admins_are_offered_the_tool_and_it_runs_first():
    src = (ROOT / "discord_bot/cogs/ai_tools.py").read_text()
    assert "getattr(interaction.permissions, ai_scam_lookup.REQUIRED_PERMISSION" in src
    assert src.index("lookups = [c for c in calls") < src.index("_expand_skill_calls(ctx, calls)") < src.index("ai_bulk.pick_tool_calls(calls)")


# ---- scam link + image from the archived evidence copy -------------------------------------------------------------

class FakeMsg:
    def __init__(self, content, atts=()):
        self.content, self.attachments = content, list(atts)


def fake_bot(msg=None, boom=False):
    class Ch:
        async def fetch_message(self, mid):
            if boom:
                raise discord.NotFound(SimpleNamespace(status=404, reason=""), "gone")
            return msg
    return SimpleNamespace(get_channel=lambda i: Ch())


def ev_hit(i=7):
    return hit(i, evidence_channel_id=1, evidence_message_id=2)


GOOD = f"\U0001F6E1\ufe0f **Scam Shield catch** | domain: `x` | server `9` | user `{UID}` | deleted\n>>> free nitro https://fatowin.com/claim?x=1 now"
IMG = SimpleNamespace(url="https://cdn.example/scam.png", content_type="image/png", filename="scam_0_a.png")


def test_evidence_gives_defanged_link_and_image():
    h = ev_hit()
    ev = run(sl.load_evidence(fake_bot(FakeMsg(GOOD, [IMG])), 9, UID, [h]))
    assert ev[7]["images"] == ["https://cdn.example/scam.png"]
    assert ev[7]["links"] and "https://" not in ev[7]["links"][0] and "hxxps://fatowin[.]com" in ev[7]["links"][0]
    e = sl.build_embed(UID, data(here_total=1, hits=[h]), {"in_server": False}, ev)
    assert e.image.url == "https://cdn.example/scam.png"
    assert "scam link:" in _text(e) and "https://fatowin" not in _text(e)


def test_evidence_from_another_server_or_user_is_never_shown():
    for bad in (GOOD.replace("server `9`", "server `10`"), GOOD.replace(f"user `{UID}`", "user `5`")):
        ev = run(sl.load_evidence(fake_bot(FakeMsg(bad, [IMG])), 9, UID, [ev_hit()]))
        assert ev == {}


def test_missing_evidence_copy_is_reported_not_raised():
    h = ev_hit()
    ev = run(sl.load_evidence(fake_bot(boom=True), 9, UID, [h]))
    assert ev[7]["gone"] is True
    assert "no longer available" in _text(sl.build_embed(UID, data(here_total=1, hits=[h]), {"in_server": False}, ev))
    assert run(sl.load_evidence(None, 9, UID, [h])) == {}


def test_domain_catch_without_a_copy_still_shows_the_matched_link():
    e = sl.build_embed(UID, data(here_total=1, hits=[hit(snippet=None)]), {"in_server": False})
    assert "scam link:" in _text(e) and "fatowin[.]com" in _text(e)


def test_extra_images_get_their_own_embeds_and_are_capped():
    hits = [ev_hit(i) for i in range(1, 6)]
    ev = {i: {"links": [], "images": [f"https://cdn.example/{i}.png"], "gone": False} for i in range(1, 6)}
    extra = sl.image_embeds(data(hits=hits), ev)
    assert len(extra) == sl.MAX_EXTRA_IMAGE_EMBEDS and extra[0].image.url.endswith("/2.png")


def test_only_a_few_copies_are_opened():
    hits = [ev_hit(i) for i in range(1, 7)]
    ev = run(sl.load_evidence(fake_bot(FakeMsg(GOOD, [IMG])), 9, UID, hits))
    assert len(ev) == sl.MAX_EVIDENCE_FETCH
