"""/welcome custombg opens the Customize Card wizard instead of taking its own url/image options."""
import asyncio
from types import SimpleNamespace

from discord_bot.cogs import welcome as w


def _command():
    return w.WelcomeCog.custombg


def test_the_command_has_no_url_or_image_options_any_more():
    cmd = _command()
    assert cmd.name == "custombg" and cmd.parent.name == "welcome"
    assert [p.name for p in cmd.parameters] == []
    assert "wizard" in cmd.description.lower()


def _it(allowed):
    sent = {"deferred": None, "msgs": []}

    class Resp:
        def __init__(self):
            self._done = False

        def is_done(self):
            return self._done

        async def defer(self, **kw):
            sent["deferred"] = kw
            self._done = True

        async def send_message(self, msg, **kw):
            sent["msgs"].append((msg, kw))
            self._done = True
    it = SimpleNamespace(guild_id=77, response=Resp(), followup=SimpleNamespace(send=None), user=SimpleNamespace(id=5),
                         permissions=SimpleNamespace(manage_guild=allowed, administrator=False), client=None, guild=None)
    return it, sent


def test_it_opens_the_wizard_for_this_server_when_allowed(monkeypatch):
    opened = []

    async def fake_open(interaction, guild_id, clone_id):
        opened.append((guild_id, clone_id))
    from discord_bot.cogs import _views_card_customize as cc
    monkeypatch.setattr(cc, "open_customize_wizard", fake_open)
    monkeypatch.setattr(w, "_require_perm", lambda i, p: True)
    monkeypatch.setattr(w, "_clone_id_of", lambda i: None)
    it, sent = _it(True)
    asyncio.run(_command().callback(object.__new__(w.WelcomeCog), it))
    assert opened == [(77, None)] and sent["deferred"] == {"ephemeral": True, "thinking": True}


def test_it_refuses_without_manage_server_and_never_opens_the_wizard(monkeypatch):
    opened = []

    async def fake_open(*a, **k):
        opened.append(1)
    from discord_bot.cogs import _views_card_customize as cc
    monkeypatch.setattr(cc, "open_customize_wizard", fake_open)
    monkeypatch.setattr(w, "_require_perm", lambda i, p: False)
    it, sent = _it(False)
    asyncio.run(_command().callback(object.__new__(w.WelcomeCog), it))
    assert opened == [] and "Manage Server" in sent["msgs"][0][0] and sent["msgs"][0][1]["ephemeral"] is True
