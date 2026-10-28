"""Server-wide level card: Premium servers only, Manage Server only, own card wins, stored under a server key."""
import asyncio
import json
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_level_card as vlc
from modules import card_assets
from modules import level_card_design as lcd

from tests.unit.test_level_card_access import GuildDB


def run(c):
    return asyncio.run(c)


SERVER = json.dumps({"accent": "#00FF00"})
PNG = b"\x89PNG-server-bg"


def server_db(premium=True, with_design=True, bg_status="approved", own=None):
    d = GuildDB(premium=premium)
    d.cards.pop("7", None)
    if own:
        d.cards["7"] = own
        d.ent["card_plan"] = {"product": "card_plan", "status": "active", "source": "t", "cancel_at_period_end": False,
                              "expires_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                              + __import__("datetime").timedelta(days=5)}
    if with_design:
        d.cards[lcd.server_key(1)] = json.dumps({"accent": "#00FF00", "custom_bg": True})
        d.assets[(lcd.server_key(1), "background")] = {"data": PNG, "status": bg_status}
    return d


def test_server_key_never_collides_with_a_user_id():
    assert lcd.server_key(123) == "g:123" and not lcd.server_key(123).isdigit()


def test_server_design_needs_premium_and_valid_data():
    assert run(lcd.server_design(server_db(), 1))["accent"] == "#00FF00"
    assert run(lcd.server_design(server_db(premium=False), 1)) is None            # lapsed / never premium: kept, not shown
    assert run(lcd.server_design(server_db(with_design=False), 1)) is None
    bad = server_db()
    bad.cards[lcd.server_key(1)] = "{not json"
    assert run(lcd.server_design(bad, 1)) is None
    assert run(lcd.server_design(server_db(), None)) is None


def test_member_without_a_card_gets_the_server_card():
    d, bg, logo = run(lcd.card_for_user(server_db(), 7, guild_id=1))
    assert d["accent"] == "#00FF00" and bg == PNG and logo is None


def test_members_own_card_wins_over_the_server_card():
    d, bg, _ = run(lcd.card_for_user(server_db(own='{"accent":"#FF0000"}'), 7, guild_id=1))
    assert d["accent"] == "#FF0000" and bg is None


def test_server_card_is_not_shown_without_premium_or_elsewhere():
    assert run(lcd.card_for_user(server_db(premium=False), 7, guild_id=1)) is None
    assert run(lcd.card_for_user(server_db(), 7)) is None                          # DM / no server
    assert run(lcd.card_for_user(server_db(), 7, guild_id=2)) is None              # another server's card is never used


def test_only_approved_server_images_are_drawn():
    _, bg, _ = run(lcd.card_for_user(server_db(bg_status="pending"), 7, guild_id=1))
    assert bg is None
    _, bg, _ = run(lcd.card_for_user(server_db(bg_status="rejected"), 7, guild_id=1))
    assert bg is None


# ── the designer ─────────────────────────────────────────────────────────────
class Resp:
    def __init__(self): self.sent, self.deferred = [], False
    async def defer(self, **kw): self.deferred = True
    async def send_message(self, *a, **kw): self.sent.append(kw | {"args": a})


class Inter:
    def __init__(self, manage=True, uid=7):
        self.user = SimpleNamespace(id=uid, display_name="Max", mention=f"<@{uid}>",
                                    guild_permissions=SimpleNamespace(manage_guild=manage))
        self.guild_id, self.client, self.channel = 1, SimpleNamespace(clone_id=None), None
        self.response, self.followup, self.edits, self.private = Resp(), self, [], []

    async def send(self, *a, **kw): self.private.append(kw | {"args": a})
    async def edit_original_response(self, **kw): self.edits.append(kw)


def with_db(monkeypatch, fake):
    monkeypatch.setattr(vlc, "db", fake)
    return fake


def test_switch_shown_only_to_admins_of_a_premium_server(monkeypatch):
    with_db(monkeypatch, server_db())
    assert run(vlc.can_edit_server(Inter(manage=True))) is True
    assert run(vlc.can_edit_server(Inter(manage=False))) is False                  # members never see it
    with_db(monkeypatch, server_db(premium=False))
    assert run(vlc.can_edit_server(Inter(manage=True))) is False                   # no Premium, no server card

    async def labels(manage):
        d = await vlc.build_draft(Inter(manage=manage))
        return [c.label for c in vlc.CardWizardView(7, d).walk_children() if getattr(c, "label", None)]
    with_db(monkeypatch, server_db())
    assert "Server card" in run(labels(True)) and "Server card" not in run(labels(False))


def test_server_draft_and_embed(monkeypatch):
    with_db(monkeypatch, server_db())
    d = run(vlc.build_draft(Inter(), "server"))
    assert d.scope == "server" and d.key == "g:1" and d.has_pass and d.design["accent"] == "#00FF00" and d.saved["background"] == PNG
    assert "server" in vlc.build_embed(d).title.lower()
    plain = run(vlc.build_draft(Inter(manage=False), "server"))                    # a member asking for it just gets their own
    assert plain.scope == "user"


def test_saving_the_server_card_goes_under_the_server_key(monkeypatch):
    fake = with_db(monkeypatch, server_db())
    i = Inter()

    async def go():
        d = await vlc.build_draft(i, "server")
        d.design = {**d.design, "accent": "#112233"}
        await vlc.save_draft(i, d)
    run(go())
    assert json.loads(fake.cards["g:1"])["accent"] == "#112233"
    assert "7" not in fake.cards                                                    # never written to the admin's own card


def test_save_is_rechecked_and_refused_when_rights_or_premium_are_gone(monkeypatch):
    fake = with_db(monkeypatch, server_db())

    async def go(inter_after, premium_after):
        i = Inter()
        d = await vlc.build_draft(i, "server")
        d.design = {**d.design, "accent": "#ABCDEF"}
        fake.premium = premium_after
        await vlc.save_draft(inter_after, d)
    before = fake.cards["g:1"]
    run(go(Inter(manage=False), True))                                              # admin lost Manage Server meanwhile
    assert fake.cards["g:1"] == before
    i2 = Inter()
    run(go(i2, False))                                                              # server's Premium ran out meanwhile
    assert fake.cards["g:1"] == before and i2.private and "Manage Server" in i2.private[0]["args"][0]


def test_server_uploads_use_the_server_key(monkeypatch):
    fake = with_db(monkeypatch, server_db(with_design=False))
    seen = []

    async def fake_submit(db, uid, kind, raw):
        seen.append(uid)
        return {"ok": True, "status": "approved", "reason": ""}

    async def no_host(*a, **k):
        return None
    monkeypatch.setattr(card_assets, "submit", fake_submit)
    monkeypatch.setattr(vlc, "_send_to_host", no_host)
    i = Inter()

    async def go():
        d = await vlc.build_draft(i, "server")
        d.staged["background"] = (b"raw", b"png")
        d.design = {**d.design, "custom_bg": True}
        await vlc.save_draft(i, d)
    run(go())
    assert seen == ["g:1"]
