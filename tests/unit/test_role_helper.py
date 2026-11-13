"""Create-the-missing-role helper, its persistent buttons, the screens that show them and the one-time reminder."""
import asyncio
import re
from types import SimpleNamespace

import discord
import pytest

from modules import role_helper as rh


def run(coro):
    return asyncio.run(coro)


class Perms:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return False


class Role:
    _next = 1000

    def __init__(self, name, position=1, perms=None, managed=False, default=False):
        Role._next += 1
        self.id, self.name, self.position = Role._next, name, position
        self.permissions, self.managed, self._default = perms or Perms(), managed, default

    mention = property(lambda self: f"<@&{self.id}>")

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position


class Guild:
    def __init__(self, manage_roles=True, bot_position=50, fail_on=None):
        self.id = 7
        self.top = Role("Bot", position=bot_position)
        self.me = SimpleNamespace(guild_permissions=Perms(manage_roles=manage_roles), top_role=self.top)
        self.roles = [Role("@everyone", 0, default=True)]
        self.created = []
        self.fail_on = fail_on or set()

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def create_role(self, name, permissions=None, reason=None):
        await asyncio.sleep(0)
        if name in self.fail_on:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "nope")
        r = Role(name, position=1, perms=permissions)
        self.roles.append(r)
        self.created.append(r)
        return r


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for the saved settings of each kind."""
    saved = {}

    async def fake_state(kind, guild_id, clone_id):
        return dict(saved.get(kind, {}))

    async def fake_apply(kind, guild, clone_id, actor_id, fields):
        saved.setdefault(kind, {}).update(fields)
    monkeypatch.setattr(rh, "_state", fake_state)
    monkeypatch.setattr(rh, "_apply", fake_apply)
    rh._locks.clear()
    return saved


# ── creating ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_creates_a_permissionless_role_and_saves_it(store):
    g = Guild()
    res = run(rh.create_missing("ticket", g, None, 5))
    assert res.ok and [r.name for r in g.created] == ["Ticket Support"]
    assert not any(vars(g.created[0].permissions).values())                      # no permissions at all
    assert store["ticket"] == {"support_role_id": g.created[0].id}
    assert "Give it to your staff" in res.text("ticket")


def test_welcome_creates_both_roles_and_only_the_missing_ones(store):
    g = Guild()
    res = run(rh.create_missing("welcome", g, None, 5))
    assert {r.name for r in g.created} == {"New Member", "Bots"} and set(store["welcome"]) == {"member_role_id", "bot_role_id"}
    store["welcome"]["bot_role_id"] = None
    g.roles = [r for r in g.roles if r.name != "Bots"]
    g.created.clear()
    run(rh.create_missing("welcome", g, None, 5))
    assert [r.name for r in g.created] == ["Bots"]                               # the member role was already set: left alone


def test_already_set_creates_nothing(store):
    g = Guild()
    r = Role("Staff")
    g.roles.append(r)
    store["ticket"] = {"support_role_id": r.id}
    res = run(rh.create_missing("ticket", g, None, 5))
    assert res.already and not g.created and "already set up" in res.text("ticket")


def test_a_deleted_role_is_made_again(store):
    g = Guild()
    store["ticket"] = {"support_role_id": 424242}                                # saved, but the role no longer exists
    run(rh.create_missing("ticket", g, None, 5))
    assert len(g.created) == 1 and store["ticket"]["support_role_id"] == g.created[0].id


def test_a_safe_same_named_role_is_reused_but_a_powerful_one_is_not(store):
    g = Guild()
    safe = Role("Ticket Support")
    g.roles.append(safe)
    res = run(rh.create_missing("ticket", g, None, 5))
    assert res.adopted == [safe] and not g.created and store["ticket"]["support_role_id"] == safe.id
    assert "reused your existing" in res.text("ticket")
    g2, store["ticket"] = Guild(), {}
    risky = Role("Ticket Support", perms=Perms(administrator=True))
    g2.roles.append(risky)
    run(rh.create_missing("ticket", g2, None, 5))
    assert len(g2.created) == 1 and store["ticket"]["support_role_id"] == g2.created[0].id != risky.id


def test_a_same_named_role_above_the_bot_is_not_reused(store):
    g = Guild(bot_position=5)
    high = Role("Ticket Support", position=9)
    g.roles.append(high)
    run(rh.create_missing("ticket", g, None, 5))
    assert len(g.created) == 1 and store["ticket"]["support_role_id"] != high.id


def test_missing_manage_roles_explains_and_creates_nothing(store):
    g = Guild(manage_roles=False)
    res = run(rh.create_missing("ticket", g, None, 5))
    assert not res.ok and "Manage Roles" in res.text("ticket") and not g.created and not store


def test_two_simultaneous_taps_create_exactly_one_role(store):
    g = Guild()

    async def go():
        return await asyncio.gather(*[rh.create_missing("ticket", g, None, 5) for _ in range(5)])
    results = run(go())
    assert len(g.created) == 1 and sum(1 for r in results if r.created) == 1 and sum(1 for r in results if r.already) == 4


def test_a_failure_on_the_second_role_still_saves_the_first(store):
    g = Guild(fail_on={"Bots"})
    res = run(rh.create_missing("welcome", g, None, 5))
    assert store["welcome"] == {"member_role_id": g.created[0].id} and not res.ok and "refused" in res.text("welcome")
    g.fail_on.clear()
    run(rh.create_missing("welcome", g, None, 5))                                # tapping again finishes the job
    assert set(store["welcome"]) == {"member_role_id", "bot_role_id"} and len(g.created) == 2


def test_unknown_kind_and_unexpected_errors_never_raise(store, monkeypatch):
    assert run(rh.create_missing("nope", Guild(), None, 5)).problem
    async def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(rh, "_state", boom)
    assert "went wrong" in run(rh.create_missing("ticket", Guild(), None, 5)).text("ticket")


# ── reminder bookkeeping ──────────────────────────────────────────────────────────────────────────────────────────
class FakeConn:
    def __init__(self, db):
        self.db = db

    async def fetchval(self, sql, key):
        if key in self.db:
            return None
        self.db[key] = "sent"
        return 1

    async def execute(self, sql, key):
        self.db.pop(key, None)

    async def fetch(self, sql, *args):
        if "discord_ticket_config" in sql:
            return [{"guild_id": g} for g in self.db.get("__cfg__", [])]
        return [{"key": k} for k in self.db if k.startswith("role_nudge:") and k.endswith(":ticket")]


class FakePool:
    def __init__(self):
        self.db = {}

    def acquire(self):
        pool = self

        class A:
            async def __aenter__(s):
                return FakeConn(pool.db)

            async def __aexit__(s, *a):
                return False
        return A()


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()

    async def gp():
        return p
    import database

    async def set_setting(key, value):
        p.db[key] = value
    monkeypatch.setattr(database, "get_pool", gp)
    monkeypatch.setattr(database.db, "set_global_setting", set_setting)
    return p


def test_claim_is_one_per_server_and_release_gives_it_back(pool):
    assert run(rh.claim(1, None, "ticket")) is True and run(rh.claim(1, None, "ticket")) is False
    assert run(rh.claim(1, 4, "ticket")) is True                                 # other clone: separate
    run(rh.release(1, None, "ticket"))
    assert run(rh.claim(1, None, "ticket")) is True
    assert "role_nudge:main:1:ticket" in pool.db and "role_nudge:4:1:ticket" in pool.db


def test_candidates_exclude_servers_already_reminded(pool):
    pool.db["__cfg__"] = [1, 2, 3]
    run(rh.claim(2, None, "ticket"))
    assert set(run(rh.ticket_candidates(None))) == {1, 3}


# ── persistent buttons ────────────────────────────────────────────────────────────────────────────────────────────
def test_every_button_id_round_trips_after_a_restart():
    from discord_bot.cogs import _views_role_helper as v
    pat = re.compile(v._PATTERN)
    for action in ("create", "make", "later"):
        for kind in rh.KINDS:
            for clone in (None, 11):
                cid = v._cid(action, kind, 123456789012345678, clone)
                m = pat.match(cid)
                assert m, cid
                b = run(v.RoleHelperButton.from_custom_id(None, None, m))
                assert (b.action, b.kind, b.guild_id, b.clone_id) == (action, kind, 123456789012345678, clone)
                assert b.item.custom_id == cid and len(cid) <= 100
    assert v.RoleHelperButton in v.DYNAMIC_ITEMS


def test_buttons_are_registered_with_the_bot_and_the_cog_is_loaded():
    src = open("discord_bot/bot.py").read()
    assert "ROLE_HELPER_DYNAMIC_ITEMS" in src and "add_dynamic_items(*ROLE_HELPER_DYNAMIC_ITEMS)" in src
    assert 'load_extension("discord_bot.cogs.role_nudge")' in src


def _inter(guild, perms, message=None):
    log = {"edits": [], "followups": [], "responses": [], "message_edits": []}

    async def defer(ephemeral=False):
        log["responses"].append("defer")

    async def edit_message(view=None):
        log["edits"].append(view)

    async def send_message(text, ephemeral=False):
        log["responses"].append(text)

    async def followup(text, ephemeral=False, allowed_mentions=None):
        log["followups"].append(text)

    async def msg_edit(view=None, allowed_mentions=None):
        log["message_edits"].append(view)
    inter = SimpleNamespace(
        guild=guild, user=SimpleNamespace(id=5, guild_permissions=perms),
        response=SimpleNamespace(defer=defer, edit_message=edit_message, send_message=send_message, is_done=lambda: bool(log["responses"])),
        followup=SimpleNamespace(send=followup), message=SimpleNamespace(edit=msg_edit))
    return inter, log


def test_button_needs_manage_server(store):
    from discord_bot.cogs import _views_role_helper as v
    g = Guild()
    inter, log = _inter(g, Perms())
    run(v.RoleHelperButton("create", "ticket", g.id, None).callback(inter))
    assert "Manage Server" in log["responses"][0] and not g.created
    inter, log = _inter(g, Perms(manage_guild=True))
    run(v.RoleHelperButton("create", "ticket", 999, None).callback(inter))      # a button from another server
    assert "Manage Server" in log["responses"][0] and not g.created


def test_create_button_makes_the_role_and_turns_the_reminder_into_the_result(store, pool):
    from discord_bot.cogs import _views_role_helper as v
    g = Guild()
    inter, log = _inter(g, Perms(manage_guild=True))
    run(v.RoleHelperButton("create", "ticket", g.id, None).callback(inter))
    assert len(g.created) == 1 and log["message_edits"] and "created" in log["followups"][0]
    assert pool.db["role_nudge:main:7:ticket"] == "done"


def test_make_button_inside_a_screen_leaves_the_screen_alone(store, pool):
    from discord_bot.cogs import _views_role_helper as v
    g = Guild()
    inter, log = _inter(g, Perms(administrator=True))
    run(v.RoleHelperButton("make", "welcome", g.id, None).callback(inter))
    assert len(g.created) == 2 and not log["message_edits"] and log["followups"]


def test_not_now_is_remembered_and_creates_nothing(store, pool):
    from discord_bot.cogs import _views_role_helper as v
    g = Guild()
    inter, log = _inter(g, Perms(manage_guild=True))
    run(v.RoleHelperButton("later", "ticket", g.id, None).callback(inter))
    assert not g.created and pool.db["role_nudge:main:7:ticket"] == "later" and log["edits"]


def test_a_failed_creation_keeps_the_reminder_so_the_admin_can_retry(store, pool):
    from discord_bot.cogs import _views_role_helper as v
    g = Guild(manage_roles=False)
    inter, log = _inter(g, Perms(manage_guild=True))
    run(v.RoleHelperButton("create", "ticket", g.id, None).callback(inter))
    assert not log["message_edits"] and "Manage Roles" in log["followups"][0] and "role_nudge:main:7:ticket" not in pool.db


# ── the screens show the button only when it is useful ────────────────────────────────────────────────────────────
def _ids(view):
    out = []
    for child in view.walk_children():
        cid = getattr(child, "custom_id", None)
        if cid:
            out.append(cid)
    return out


def test_ticket_wizard_offers_the_button_only_without_a_support_role():
    from discord_bot.cogs import _views_ticket_wizard as tw
    g = SimpleNamespace(id=7, get_role=lambda rid: object() if rid == 55 else None, get_channel=lambda i: None,
                        text_channels=[], categories=[])
    base = {"guild_id": 7, "clone_id": None, "wizard_invoker_id": 5, "support_role_id": None, "category_id": None,
            "panel_channel_id": None, "panel_message_id": None, "welcome_message": None}
    assert any(c.startswith("rolehelper:make:ticket:7:") for c in _ids(tw.build_wizard_view(g, dict(base), False)))
    assert not any(c.startswith("rolehelper:") for c in _ids(tw.build_wizard_view(g, dict(base, support_role_id=55), False)))
    assert any(c.startswith("rolehelper:make:ticket:7:") for c in _ids(tw.build_wizard_view(g, dict(base, support_role_id=99), False)))  # deleted role


# ── the one-time reminder pass ────────────────────────────────────────────────────────────────────────────────────
class FakeChannel(discord.TextChannel):
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def permissions_for(self, me):
        return SimpleNamespace(view_channel=True, send_messages=True)

    async def send(self, view=None, allowed_mentions=None):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "x")
        self.sent.append(view)


def _cog(monkeypatch, guilds, channels, candidates, pool):
    from discord_bot.cogs import role_nudge as rn
    from modules import guild_activity as ga
    real_sleep = asyncio.sleep
    monkeypatch.setattr(rn.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(ga, "tracker", ga.ActivityTracker())
    pool.db["__cfg__"] = candidates

    async def fake_cfg(gid, cid):
        return {"log_channel_id": channels.get(gid) and gid}
    monkeypatch.setattr(rn.db, "get_automod_config", fake_cfg)
    bot = SimpleNamespace(clone_id=None, guilds=guilds, user=SimpleNamespace(display_name="Prime"))
    cog = rn.RoleNudgeCog(bot)
    for g in guilds:
        g.get_channel = lambda cid, _c=channels: _c.get(cid)
    return cog, rn


def _g(i, old=True):
    from datetime import datetime, timedelta, timezone
    joined = datetime.now(timezone.utc) - timedelta(days=3 if old else 0)
    return SimpleNamespace(id=i, member_count=50, me=SimpleNamespace(joined_at=joined), get_channel=None)


def test_reminder_goes_to_servers_with_tickets_and_no_support_role_exactly_once(monkeypatch, pool):
    guilds = [_g(1), _g(2), _g(3, old=False), _g(4)]
    ch = {1: FakeChannel(), 3: FakeChannel(), 4: None}
    cog, rn = _cog(monkeypatch, guilds, ch, [1, 3, 4], pool)
    assert run(cog.nudge_pass()) == 1                                           # 2 isn't a candidate, 3 is too new, 4 has no log channel
    assert len(ch[1].sent) == 1 and not ch[3].sent
    assert run(cog.nudge_pass()) == 0 and len(ch[1].sent) == 1                  # never twice


def test_failed_post_gives_the_reminder_back_and_a_sleeping_server_is_skipped(monkeypatch, pool):
    guilds = [_g(1), _g(2)]
    ch = {1: FakeChannel(fail=True), 2: FakeChannel()}
    cog, rn = _cog(monkeypatch, guilds, ch, [1, 2], pool)
    from modules import guild_activity as ga
    now = [1_000_000.0]
    ga.tracker = ga.ActivityTracker(clock=lambda: now[0])
    ga.tracker.seed([1, 2])
    now[0] += 5 * 86400
    ga.tracker.touch(1)                                                          # 1 is awake, 2 is asleep
    run(cog.nudge_pass())
    assert "role_nudge:main:1:ticket" not in pool.db and not ch[2].sent         # post failed -> claim returned; sleeping 2 skipped
    ch[1].fail = False
    assert run(cog.nudge_pass()) == 1 and len(ch[1].sent) == 1


def test_a_deploy_never_floods_channels(monkeypatch, pool):
    guilds = [_g(i) for i in range(1, 21)]
    ch = {i: FakeChannel() for i in range(1, 21)}
    cog, rn = _cog(monkeypatch, guilds, ch, list(range(1, 21)), pool)
    assert run(cog.nudge_pass()) == rn.MAX_PER_PASS
