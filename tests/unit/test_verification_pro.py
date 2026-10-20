"""Join verification: captcha default, premium extras (custom panel, log channel, auto-kick, min account age,
welcome DM) and the rebuilt two-page wizard."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import database
from discord_bot.cogs import verification as ver
from discord_bot.cogs._views_verification import build_verify_panel_embed
from modules import verification_pro as vp


def run(coro):
    return asyncio.run(coro)


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


# ── pure rules ────────────────────────────────────────────────────────────

def test_parse_color():
    assert vp.parse_color("#5865F2") == 0x5865F2
    assert vp.parse_color("ff8800") == 0xFF8800
    assert vp.parse_color("  ") is None and vp.parse_color(None) is None
    assert vp.parse_color("#000000") == 0            # black is a real colour, not "unset"
    for bad in ("red", "#12345", "#GGGGGG", "5865F2FF"):
        with pytest.raises(ValueError):
            vp.parse_color(bad)


def test_clean_image_url():
    assert vp.clean_image_url("https://x.example/a.png") == "https://x.example/a.png"
    assert vp.clean_image_url("") is None
    for bad in ("http://x.example/a.png", "javascript:alert(1)", "https://x.example/a b.png", "https://x/" + "a" * 400):
        with pytest.raises(ValueError):
            vp.clean_image_url(bad)


def test_account_age_rule():
    assert vp.account_too_young(NOW - timedelta(days=2), 7, NOW)
    assert not vp.account_too_young(NOW - timedelta(days=7), 7, NOW)       # exactly old enough
    assert not vp.account_too_young(NOW - timedelta(minutes=1), None, NOW)  # off


def test_kick_rule():
    assert vp.should_kick(NOW - timedelta(minutes=30), 30, NOW)
    assert not vp.should_kick(NOW - timedelta(minutes=29), 30, NOW)
    assert not vp.should_kick(None, 30, NOW)                                # unknown join time is never kicked
    assert not vp.should_kick(NOW - timedelta(days=9), None, NOW)           # off


def test_free_servers_ignore_saved_extras_but_keep_them():
    cfg = {"mode": "captcha", "channel_id": 5, **{k: ("x" if k != "kick_after_minutes" else 30) for k in vp.PRO_FIELDS}}
    free = vp.effective_cfg(cfg, False)
    assert all(free[k] is None for k in vp.PRO_FIELDS)
    assert free["mode"] == "captcha" and free["channel_id"] == 5
    assert cfg["kick_after_minutes"] == 30                                  # the saved config is untouched
    assert vp.effective_cfg(cfg, True) is cfg
    assert vp.paused_extras(cfg) and not vp.paused_extras({k: None for k in vp.PRO_FIELDS})


def test_panel_look_keeps_zero_colour_and_drops_unset():
    assert vp.panel_look({"panel_color": 0, "panel_title": None, "panel_text": ""}) == {"panel_color": 0}


def test_placeholders():
    guild = SimpleNamespace(name="Cats")
    assert vp.render_placeholders("hi {user}, welcome to {server}", SimpleNamespace(mention="<@1>"), guild) == "hi <@1>, welcome to Cats"


# ── panel ─────────────────────────────────────────────────────────────────

def test_default_panel_unchanged_and_custom_panel_applies_look():
    plain = build_verify_panel_embed("Cats", "captcha")
    assert plain.title == "Welcome to Cats 👋" and "security check" in plain.description
    assert plain.color == discord.Color.green()
    custom = build_verify_panel_embed("Cats", "captcha", {
        "panel_title": "Hold up", "panel_text": "Press the button.", "panel_color": 0xFF8800,
        "panel_image_url": "https://x.example/b.png"})
    assert custom.title == "Hold up" and custom.description == "Press the button."   # no auto "security check" suffix
    assert custom.color.value == 0xFF8800 and custom.image.url == "https://x.example/b.png"


# ── captcha is the default ────────────────────────────────────────────────

class _Conn:
    def __init__(self, row=None):
        self.row, self.queries = row, []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self.row


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return _Ctx()


def _patch_pool(monkeypatch, conn):
    async def fake_get_pool():
        return _Pool(conn)
    monkeypatch.setattr(database, "get_pool", fake_get_pool)


def test_unconfigured_server_defaults_to_captcha(monkeypatch):
    _patch_pool(monkeypatch, _Conn(row=None))
    cfg = run(database.Database().get_verification_config(1, None))
    assert cfg["mode"] == "captcha"
    assert all(cfg[k] is None for k in vp.PRO_FIELDS)


def test_set_config_sql_has_one_param_per_column(monkeypatch):
    conn = _Conn(row={"guild_id": 1})
    _patch_pool(monkeypatch, conn)
    db = database.Database()

    async def fake_get(*a, **k):
        return {"mode": "captcha", **{k2: None for k2 in vp.PRO_FIELDS}}
    monkeypatch.setattr(db, "get_verification_config", fake_get)
    run(db.set_verification_config(1, None, enabled=True, kick_after_minutes=30, panel_color=0))
    sql, args = conn.queries[0]
    used = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert used == set(range(1, len(args) + 1))
    assert "kick_after_minutes" in sql and "welcome_text" in sql
    # explicit values land, omitted ones keep current, 0 is not turned into NULL
    cols = re.search(r"INSERT INTO discord_verification_config \((.*?)\)", sql, re.S).group(1).replace("\n", "").split(",")
    by_col = dict(zip([c.strip() for c in cols], args))
    assert by_col["kick_after_minutes"] == 30 and by_col["panel_color"] == 0 and by_col["enabled"] is True
    assert by_col["mode"] == "captcha"


# ── wizard layout ─────────────────────────────────────────────────────────

def _count(view):
    return sum(1 for _ in view.walk_children())


def _labels(view):
    return [getattr(c, "label", None) for c in view.walk_children() if getattr(c, "label", None)]


def _finish(view):
    return next(c for c in view.walk_children() if isinstance(c, ver.FinishButton))


def test_new_wizard_defaults_to_captcha_and_marks_it_selected():
    wiz = ver.WizardView(1, {})
    assert wiz.mode == "captcha"
    mode = next(c for c in wiz.walk_children() if isinstance(c, ver.ModeSelect))
    assert [(o.value, o.default) for o in mode.options] == [("captcha", True), ("button", False)]


def test_existing_button_mode_server_keeps_its_mode():
    wiz = ver.WizardView(1, {"mode": "button"})
    mode = next(c for c in wiz.walk_children() if isinstance(c, ver.ModeSelect))
    assert wiz.mode == "button" and [o.default for o in mode.options] == [False, True]


def test_setup_page_groups_each_create_button_with_its_picker():
    wiz = ver.WizardView(1, {})
    assert _labels(wiz).count("✨ Auto-create") == 3
    sections = [c for c in wiz.walk_children() if isinstance(c, discord.ui.Section)]
    assert len(sections) == 3 and all(isinstance(s.accessory, discord.ui.Button) for s in sections)
    assert _count(wiz) <= 40


def test_lock_down_button_needs_channel_and_unverified_role():
    assert _finish(ver.WizardView(1, {})).disabled
    assert _finish(ver.WizardView(1, {"channel_id": 5})).disabled
    assert not _finish(ver.WizardView(1, {"channel_id": 5, "unverified_role_id": 6})).disabled


def test_pickers_show_saved_choices_and_survive_a_rebuild():
    wiz = ver.WizardView(1, {"channel_id": 5, "unverified_role_id": 6, "verified_role_id": 7})
    assert [d.id for d in wiz.channel_select.default_values] == [5]
    assert [d.id for d in wiz.unverified_role_select.default_values] == [6]
    assert [d.id for d in wiz.verified_role_select.default_values] == [7]
    wiz.channel_id = 99
    wiz._build()                                  # what refresh() does after any pick or auto-create
    assert [d.id for d in wiz.channel_select.default_values] == [99]
    assert len(list(wiz.children)) == 1           # one container, nothing stacked up


def test_premium_page_layout_and_free_vs_premium_buttons():
    wiz = ver.WizardView(1, {"kick_after_minutes": 60, "min_account_age_days": 7})
    wiz.page = "pro"
    wiz._build()
    assert _count(wiz) <= 40
    assert "Get Premium" in _labels(wiz) and "Back to setup" in _labels(wiz)
    kick = next(c for c in wiz.walk_children() if isinstance(c, ver.KickAfterSelect))
    assert [o.label for o in kick.options if o.default] == ["1 hour"]
    age = next(c for c in wiz.walk_children() if isinstance(c, ver.MinAgeSelect))
    assert [o.label for o in age.options if o.default] == ["7 days"]
    wiz.premium = True
    wiz._build()
    assert "Get Premium" not in _labels(wiz)


class _Resp:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, *a, **k):
        self.sent.append((a, k))

    async def edit_message(self, **k):
        self.edited.append(k)


def _inter(user_id=1, guild_id=10):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild_id=guild_id, response=_Resp(),
                           client=SimpleNamespace(clone_id=None))


def test_only_the_invoker_can_use_the_wizard():
    wiz, bad = ver.WizardView(1, {}), _inter(user_id=2)
    assert run(wiz.interaction_check(bad)) is False and bad.response.sent
    assert run(wiz.interaction_check(_inter(user_id=1))) is True


def test_choice_selects_store_values_and_off_clears():
    wiz = ver.WizardView(1, {"kick_after_minutes": 30})
    wiz.page = "pro"
    wiz._build()
    sel = next(c for c in wiz.walk_children() if isinstance(c, ver.KickAfterSelect))
    inter = _inter()
    sel._values = ["360"]
    run(sel.callback(inter))
    assert wiz.pro["kick_after_minutes"] == 360 and inter.response.edited
    sel._values = ["off"]
    run(sel.callback(_inter()))
    assert wiz.pro["kick_after_minutes"] is None


def test_look_modal_validates_then_saves():
    wiz = ver.WizardView(1, {})
    modal = ver.LookModal(wiz)
    modal.title_in._value, modal.text_in._value = "Hold up", "Press it"
    modal.color_in._value, modal.image_in._value = "nope", ""
    modal.welcome_in._value = "Hi {user}"
    bad = _inter()
    run(modal.on_submit(bad))
    assert bad.response.sent and not bad.response.edited and wiz.pro["panel_title"] is None   # nothing saved on error
    modal.color_in._value = "#FF8800"
    ok = _inter()
    run(modal.on_submit(ok))
    assert ok.response.edited
    assert wiz.pro["panel_title"] == "Hold up" and wiz.pro["panel_color"] == 0xFF8800 and wiz.pro["welcome_text"] == "Hi {user}"
    assert wiz.pro["panel_image_url"] is None


# ── join / kick behaviour ─────────────────────────────────────────────────

class _Member:
    def __init__(self, uid, created, joined=None, roles=(), top=1, bot=False):
        self.id, self.created_at, self.joined_at, self.roles, self.bot = uid, created, joined, list(roles), bot
        self.mention = f"<@{uid}>"
        self.top_role = _Role(top)
        self.kicked, self.dms, self.added = None, [], []
        self.guild = None

    async def kick(self, reason=None):
        self.kicked = reason

    async def send(self, text):
        self.dms.append(text)

    async def add_roles(self, *roles, reason=None):
        self.added += roles


class _Role:
    def __init__(self, position, members=()):
        self.position, self.members = position, list(members)

    def __ge__(self, other):
        return self.position >= other.position


class _Channel:
    def __init__(self):
        self.posts = []

    async def send(self, text, **k):
        self.posts.append(text)


def _guild(members=(), kick_perm=True, verified=None):
    unv = _Role(1, members)
    log = _Channel()
    me = SimpleNamespace(top_role=_Role(50), guild_permissions=SimpleNamespace(kick_members=kick_perm))
    g = SimpleNamespace(id=10, name="Cats", me=me, owner=SimpleNamespace(id=999), unv=unv, log=log,
                        get_role=lambda rid: {6: unv, 7: verified}.get(rid),
                        get_channel=lambda cid: log if cid == 20 else None)
    for m in members:
        m.guild = g
    return g


def _cog():
    cog = ver.VerificationCog.__new__(ver.VerificationCog)
    cog.bot = SimpleNamespace(clone_id=None)
    return cog


def _premium(monkeypatch, value):
    async def fake(guild_id, clone_id):
        return value
    monkeypatch.setattr(vp, "is_premium", fake)


ROW = {"guild_id": 10, "unverified_role_id": 6, "verified_role_id": 7, "log_channel_id": 20,
       "kick_after_minutes": 30, "enabled": True}


def test_auto_kick_removes_only_stale_unverified_members_and_logs(monkeypatch):
    _premium(monkeypatch, True)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    stale, recent, a_bot = _Member(1, old, old), _Member(2, old, fresh), _Member(3, old, old, bot=True)
    senior = _Member(4, old, old, top=60)                         # outranks the bot: can't be kicked, must be skipped
    guild = _guild([stale, recent, a_bot, senior])
    cog = _cog()
    cog.bot.get_guild = lambda gid: guild

    async def no_sleep(_):
        return None
    monkeypatch.setattr(ver.asyncio, "sleep", no_sleep)
    assert run(cog._kick_stale_in(dict(ROW), None)) == 1
    assert stale.kicked and "30 minutes" in stale.kicked and stale.dms
    assert recent.kicked is None and a_bot.kicked is None and senior.kicked is None
    assert any("Kicked" in p and "<@1>" in p for p in guild.log.posts)


def test_auto_kick_is_off_for_free_servers_even_with_it_saved(monkeypatch):
    _premium(monkeypatch, False)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    m = _Member(1, old, old)
    guild = _guild([m])
    cog = _cog()
    cog.bot.get_guild = lambda gid: guild
    assert run(cog._kick_stale_in(dict(ROW), None)) == 0 and m.kicked is None


def test_auto_kick_skips_members_who_already_have_the_verified_role(monkeypatch):
    _premium(monkeypatch, True)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    verified = _Role(2)
    m = _Member(1, old, old, roles=[verified])
    guild = _guild([m], verified=verified)
    cog = _cog()
    cog.bot.get_guild = lambda gid: guild
    assert run(cog._kick_stale_in(dict(ROW), None)) == 0 and m.kicked is None


def test_auto_kick_caps_kicks_per_pass(monkeypatch):
    _premium(monkeypatch, True)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    members = [_Member(i, old, old) for i in range(40)]
    guild = _guild(members)
    cog = _cog()
    cog.bot.get_guild = lambda gid: guild

    async def no_sleep(_):
        return None
    monkeypatch.setattr(ver.asyncio, "sleep", no_sleep)
    assert run(cog._kick_stale_in(dict(ROW), None)) == ver.VerificationCog.MAX_KICKS_PER_PASS


def _join(monkeypatch, premium, created_days_ago, min_days=7):
    _premium(monkeypatch, premium)
    cfg = {**ROW, "min_account_age_days": min_days}

    async def fake_cfg(guild_id, clone_id=None):
        return cfg
    monkeypatch.setattr(ver.db, "get_verification_config", fake_cfg)
    member = _Member(1, datetime.now(timezone.utc) - timedelta(days=created_days_ago))
    member.guild = _guild([member])
    run(_cog().on_member_join(member))
    return member


def test_new_account_is_kicked_on_join_for_premium(monkeypatch):
    m = _join(monkeypatch, premium=True, created_days_ago=1)
    assert m.kicked and m.dms and not m.added
    assert any("younger than 7 day" in p for p in m.guild.log.posts)


def test_old_enough_account_gets_the_unverified_role(monkeypatch):
    m = _join(monkeypatch, premium=True, created_days_ago=30)
    assert m.kicked is None and list(m.added) == [m.guild.unv]


def test_free_server_never_kicks_for_account_age(monkeypatch):
    m = _join(monkeypatch, premium=False, created_days_ago=1)
    assert m.kicked is None and list(m.added) == [m.guild.unv]


def test_on_verified_logs_and_sends_welcome(monkeypatch):
    member = _Member(1, NOW, NOW)
    member.guild = _guild([member])
    sent = run(vp.on_verified(member, {"log_channel_id": 20, "welcome_text": "Hi {user}, welcome to {server}!"}))
    assert sent and member.dms == ["Hi <@1>, welcome to Cats!"]
    assert any("Verified" in p for p in member.guild.log.posts)
    quiet = _Member(2, NOW, NOW)
    quiet.guild = member.guild
    assert run(vp.on_verified(quiet, {})) is False and not quiet.dms
