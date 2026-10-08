import asyncio
from types import SimpleNamespace

import pytest

import config
from utils import dash_links as L


def test_urls():
    assert L.dashboard_url() == f"{config.DASH_PAGES_URL}/"
    assert L.dashboard_url(123) == f"{config.DASH_PAGES_URL}/#/g/123"
    assert L.dashboard_url("456") .endswith("/#/g/456")


def test_no_secret_in_link():
    assert "token" not in L.dashboard_url(123)


def test_clone_bots_are_not_supported_yet():
    assert L.dashboard_supported(None) and not L.dashboard_supported(7)
    assert L.dashboard_link_button(1, clone_id=7) is None
    b = L.dashboard_link_button(1)
    assert b.url.endswith("/#/g/1") and b.style.name == "link"


def test_server_panel_masked_link():
    from discord_bot.cogs._views_server_panel import _site_links
    assert "[Web dashboard](" in _site_links(55, None)
    assert "Web dashboard" not in _site_links(55, 3)      # clones keep the old behaviour
    assert "Web dashboard" not in _site_links(None, None)  # inspecting another server: no link


# --- /dashboard command -------------------------------------------------------

class FakeResponse:
    def __init__(self): self.sent, self.deferred = None, False
    async def send_message(self, text, view=None, ephemeral=False): self.sent = (text, view, ephemeral)
    async def defer(self, ephemeral=False): self.deferred = True


def _interaction(perms, clone_id=None):
    r = FakeResponse()
    follow = SimpleNamespace(sent=None)
    async def fsend(text, view=None, ephemeral=False): follow.sent = (text, view, ephemeral)
    follow.send = fsend
    i = SimpleNamespace(permissions=perms, guild_id=999, response=r, followup=follow,
                        client=SimpleNamespace(clone_id=clone_id))
    return i, r, follow


def _run(cog, i):
    return asyncio.run(cog.dashboard.callback(cog, i))


def test_dashboard_command_requires_manage_server():
    from discord_bot.cogs.dashboard import DashboardCog
    i, r, _ = _interaction(SimpleNamespace(administrator=False, manage_guild=False))
    _run(DashboardCog(None), i)
    assert "Manage Server" in r.sent[0] and r.sent[1] is None and r.sent[2] is True


@pytest.mark.parametrize("perms", [SimpleNamespace(administrator=True, manage_guild=False),
                                    SimpleNamespace(administrator=False, manage_guild=True)])
def test_dashboard_command_main_bot_gets_the_new_link(perms):
    from discord_bot.cogs.dashboard import DashboardCog
    i, r, _ = _interaction(perms)
    _run(DashboardCog(None), i)
    text, view, eph = r.sent
    assert eph is True
    assert view.children[0].url == f"{config.DASH_PAGES_URL}/#/g/999" and "token" not in view.children[0].url


def test_dashboard_command_clone_keeps_legacy_link(monkeypatch):
    from discord_bot.cogs import dashboard as D
    async def tok(gid, clone_id=None): return "SECRET"
    monkeypatch.setattr(D, "db", SimpleNamespace(get_or_create_dashboard_token=tok))
    i, r, follow = _interaction(SimpleNamespace(administrator=True, manage_guild=False), clone_id=4)
    _run(D.DashboardCog(None), i)
    assert r.deferred and "token=SECRET" in follow.sent[1].children[0].url and follow.sent[2] is True
