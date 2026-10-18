"""Join DM: the Scam Shield button replaced "List your server"; its sub-screen; what happens to a scam sender."""
import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot import bot as bot_mod
from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import _views_join_dm_scamshield as vs
from modules import scam_shield as ss
from modules import scam_shield_actions as sa

KEYS = list(jd.FEATURE_TOGGLES.keys())
PAGES = -(-len(KEYS) // jd.FEATURES_PER_PAGE)
OFFER = {"show_listing": False, "show_invite": False, "show_disguild": True,
         "disguild_url": "https://disguild.pages.dev/submit/server?utm_source=join-dm"}


def run(c):
    return asyncio.run(c)


def _view(page=0, offer=None, **kw):
    return jd.build_join_dm_view(1, None, KEYS, page, intro="intro", notices=[("A", "b"), ("C", "d")],
                                 join_offer=offer, guild_name="G", **kw)


def _buttons(view):
    """Plain buttons and the Button inside every DynamicItem (what walk_children hands back for those)."""
    found = []
    for c in view.walk_children():
        c = c.item if isinstance(c, discord.ui.DynamicItem) else c
        if isinstance(c, discord.ui.Button):
            found.append(c)
    return found


def _scam_buttons(view):
    return [b for b in _buttons(view) if (b.custom_id or "").startswith("join_dm_scam:")]


# ── the join DM ─────────────────────────────────────────────────────────────
def test_scam_shield_button_is_on_page_one_only():
    assert len(_scam_buttons(_view(0))) == 1
    for page in range(1, PAGES):
        assert _scam_buttons(_view(page)) == []


def test_list_your_server_button_is_gone_even_with_the_old_claim():
    for page in range(PAGES):
        for offer in (None, OFFER):
            links = [b for b in _buttons(_view(page, offer)) if b.style == discord.ButtonStyle.link]
            assert not any("list your server" in (b.label or "").lower() for b in links)
            assert not any("disguild" in (b.url or "") for b in links)


def test_every_page_stays_inside_discords_component_limit():
    ad = {"company_name": "Co", "ad_title": "T", "ad_description": "D https://x.y", "target_url": "https://x.y",
          "image_url": "https://x.y/i.png"}
    both = {"show_listing": True, "show_invite": True}
    for page in range(PAGES):
        assert len(list(_view(page, both, fresh_ad=ad).walk_children())) <= 40


def test_open_button_is_registered_and_round_trips():
    assert vs.OpenScamShieldButton in jd.DYNAMIC_ITEMS and vs.ScamSubButton in jd.DYNAMIC_ITEMS
    for clone in (None, 7):
        cid = vs.OpenScamShieldButton(123, clone).item.custom_id
        m = re.match(vs._OPEN_PAT, cid)
        assert m and int(m.group("guild")) == 123 and vs._clone_from(m.group("clone")) == clone


# ── the Scam Shield screen ──────────────────────────────────────────────────
def _state(premium=False, action="delete", enabled=True, strict=False):
    return {"scam": {"cfg": {"enabled": enabled, "allowed_domains": [], "punish_action": action}, "caught": 3, "global_on": True},
            "premium": premium, "strict": strict}


def _guild():
    return SimpleNamespace(id=5, me=None)


def _screen(**kw):
    return vs.build_view(vs.build_container(_guild(), None, _state(**kw)))


def test_screen_has_the_picture_and_every_option():
    view = _screen()
    assert any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())
    ids = {b.custom_id.split(":")[1] for b in _buttons(view)}
    assert {"act_delete", "act_timeout", "act_quarantine", "act_kick", "act_ban",
            "toggle", "check", "allow", "recent", "strict", "deep", "upgrade", "back"} <= ids
    assert vs.IMAGE_PATH.exists()


def test_screen_marks_the_chosen_action_and_disables_it():
    for action in ss.PUNISH_ACTIONS:
        acts = [b for b in _buttons(_screen(action=action)) if b.custom_id.startswith("joindm_scam:act_")]
        picked = [b for b in acts if b.custom_id.startswith(f"joindm_scam:act_{action}:")]
        others = [b for b in acts if b not in picked]
        assert len(picked) == 1 and len(others) == len(ss.PUNISH_ACTIONS) - 1
        assert picked[0].disabled and picked[0].style == discord.ButtonStyle.success and picked[0].label.endswith("✓")
        assert not any(b.disabled for b in others)


def test_premium_servers_do_not_see_the_pitch_or_upgrade_button():
    free = {b.custom_id.split(":")[1] for b in _buttons(_screen(premium=False))}
    paid = {b.custom_id.split(":")[1] for b in _buttons(_screen(premium=True))}
    assert "upgrade" in free and "upgrade" not in paid
    assert [b for b in _buttons(_screen(premium=False)) if b.custom_id.startswith("joindm_scam:strict")][0].label.endswith("🔒")
    assert not [b for b in _buttons(_screen(premium=True)) if b.custom_id.startswith("joindm_scam:strict")][0].label.endswith("🔒")


def test_screen_stays_inside_the_component_limit_and_row_limits():
    for premium in (False, True):
        view = _screen(premium=premium)
        assert len(list(view.walk_children())) <= 40
        for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


def test_sub_button_custom_ids_round_trip_and_fit_discords_limit():
    for kind in vs.KINDS:
        for clone in (None, 12):
            cid = vs.ScamSubButton(kind, 1234567890123456789, clone).item.custom_id
            m = re.match(vs._PAT, cid)
            assert m and m.group("kind") == kind and len(cid) <= 100


def test_every_punish_action_has_a_button_and_a_description():
    assert set(sa.ACTIONS) == set(ss.PUNISH_ACTIONS) and ss.PUNISH_ACTIONS[0] == "delete"
    assert set(vs.ACTION_KINDS) == {f"act_{a}" for a in ss.PUNISH_ACTIONS}


# ── what happens to the sender ──────────────────────────────────────────────
def _member(top=1, bot=False, owner=False, perms=None):
    m = MagicMock(spec=discord.Member)
    m.bot, m.id = bot, 99
    m.top_role = top
    m.guild_permissions = SimpleNamespace(**{p: False for p in
                          ("administrator", "manage_guild", "ban_members", "kick_members", "moderate_members", "manage_messages")} | (perms or {}))
    m.timeout = AsyncMock()
    return m


def _srv(me_top=10, perms=None, owner_id=1):
    me = SimpleNamespace(top_role=me_top, guild_permissions=SimpleNamespace(**(
        {"ban_members": True, "kick_members": True, "moderate_members": True, "manage_roles": True} | (perms or {}))))
    g = MagicMock()
    g.id, g.owner_id, g.me = 5, owner_id, me
    g.ban, g.kick = AsyncMock(), AsyncMock()
    return g


def test_delete_only_does_nothing_to_the_sender():
    g, m = _srv(), _member()
    assert run(sa.punish(g, None, m, "delete")) == (False, "")
    assert run(sa.punish(g, None, m, "nonsense")) == (False, "")
    g.ban.assert_not_called(); g.kick.assert_not_called(); m.timeout.assert_not_called()


def test_ban_kick_and_timeout_act_once():
    g, m = _srv(), _member()
    ok, note = run(sa.punish(g, None, m, "ban"))
    assert ok and "banned" in note and g.ban.await_args.kwargs["delete_message_seconds"] == 3600
    assert run(sa.punish(g, None, m, "kick"))[0] and g.kick.await_count == 1
    assert run(sa.punish(g, None, m, "timeout"))[0]
    assert m.timeout.await_args.args[0] == timedelta(hours=ss.TIMEOUT_HOURS)


def test_staff_owner_bots_webhooks_and_higher_roles_are_never_actioned():
    g = _srv()
    cases = [_member(bot=True), _member(perms={"ban_members": True}), _member(perms={"manage_messages": True}),
             _member(top=10), _member(top=11), SimpleNamespace(bot=False, id=1)]
    owner = _member(); owner.id = 1
    cases.append(owner)
    for m in cases:
        ok, note = run(sa.punish(g, None, m, "ban"))
        assert not ok and note.startswith("No extra action")
    g.ban.assert_not_called()


def test_missing_permission_is_reported_not_raised():
    g = _srv(perms={"ban_members": False})
    ok, note = run(sa.punish(g, None, _member(), "ban"))
    assert not ok and "Ban Members" in note
    assert sa.missing_permission(g, "ban") and sa.missing_permission(g, "delete") is None


def test_discord_refusal_is_reported_not_raised():
    g = _srv()
    g.ban = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no"))
    ok, note = run(sa.punish(g, None, _member(), "ban"))
    assert not ok and "Couldn't ban" in note


# ── the send path no longer claims or shows the Disguild reminder ───────────
class FakeDb:
    def __init__(self):
        self.claimed = set()

    async def is_join_dm_dismissed(self, guild_id, clone_id):
        return False

    async def claim_auto_listing_offer_send(self, guild_id, clone_id):
        return False

    async def claim_disguild_list_reminder(self, guild_id):
        self.claimed.add(guild_id)
        return True

    async def mark_quickstart_dm_sent(self, *a, **k):
        pass


def _send(db, monkeypatch, *, dm_fails=False):
    owner = SimpleNamespace(bot=False, send=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="x"), "closed") if dm_fails else None))
    channel = SimpleNamespace(send=AsyncMock())
    content = {"has_quickstart_cog": False, "feature_keys": KEYS, "intro": "i", "title": "t", "notices": []}

    async def build_view(guild, clone_id, content, join_offer=None, dm=False, compact=False):
        return jd.build_join_dm_view(guild.id, clone_id, content["feature_keys"], 0, intro="i", join_offer=join_offer, guild_name=guild.name)

    me = SimpleNamespace(clone_id=None, _build_join_dm_content=AsyncMock(return_value=content), _build_join_dm_view=build_view)
    monkeypatch.setattr(bot_mod, "db", db)
    monkeypatch.setattr(bot_mod, "resolve_guild_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(jd, "_default_text_channel", lambda guild: channel)
    run(bot_mod.AnimeBotDiscord._send_combined_owner_join_dm(me, SimpleNamespace(id=777, name="G"), is_initial_send=True))
    return owner, channel


def test_join_dm_and_backup_copy_carry_scam_shield_and_never_burn_the_reminder_claim(monkeypatch):
    db = FakeDb()
    owner, channel = _send(db, monkeypatch)
    assert [len(_scam_buttons(c.kwargs["view"])) for c in owner.send.call_args_list] == [1]
    assert [len(_scam_buttons(c.kwargs["view"])) for c in channel.send.call_args_list] == [1]
    assert db.claimed == set()


# ── storage ─────────────────────────────────────────────────────────────────
def test_the_action_column_is_migrated_and_wired_into_startup():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sql = root.joinpath("database/migrations/054_scam_shield_action.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS punish_action TEXT NOT NULL DEFAULT 'delete'" in sql
    src = root.joinpath("database.py").read_text(encoding="utf-8")
    assert "054_scam_shield_action.sql" in src
    assert ss._DEFAULT_GUILD["punish_action"] == "delete"
