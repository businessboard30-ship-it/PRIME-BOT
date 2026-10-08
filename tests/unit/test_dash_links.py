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


def test_clone_bots_are_supported():
    assert L.dashboard_supported(None) and L.dashboard_supported(7)
    assert L.dashboard_url(5, 7).endswith("/#/c/7/g/5")
    assert L.dashboard_link_button(1, clone_id=7).url.endswith("/#/c/7/g/1")
    b = L.dashboard_link_button(1)
    assert b.url.endswith("/#/g/1") and b.style.name == "link"


def test_server_panel_masked_link():
    from discord_bot.cogs._views_server_panel import _site_links
    assert "[Web dashboard](" in _site_links(55, None)
    assert "/#/c/3/g/55" in _site_links(55, 3)
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


def test_dashboard_command_clone_gets_clone_link():
    from discord_bot.cogs import dashboard as D
    i, r, follow = _interaction(SimpleNamespace(administrator=True, manage_guild=False), clone_id=4)
    _run(D.DashboardCog(None), i)
    url = r.sent[1].children[0].url
    assert url == f"{config.DASH_PAGES_URL}/#/c/4/g/999" and "token" not in url


# --- deep links + views (join DM, server panel, honeypot) ---------------------

def test_module_deep_link():
    assert L.dashboard_url(5, None, "honeypot").endswith("/#/g/5/honeypot")
    assert L.dashboard_url(5, 7, "honeypot").endswith("/#/c/7/g/5/honeypot")
    assert L.dashboard_link_button(5, module="welcome").url.endswith("/#/g/5/welcome")


def test_module_deep_link_rejects_junk():
    # the dashboard router only accepts [a-z_]+; anything else falls back to the overview
    assert L.dashboard_url(5, None, "../x?y=1").endswith("/#/g/5")
    assert L.dashboard_url(5, None, "Honey Pot").endswith("/#/g/5")


def _walk_text(view):
    out = []
    def go(item):
        if hasattr(item, "content"):
            out.append(item.content)
        for c in getattr(item, "children", []) or []:
            go(c)
    for it in view.children:
        go(it)
    return "\n".join(out)


def test_honeypot_panel_has_masked_dashboard_link():
    from discord_bot.cogs import honeypot as hp
    line = hp._dash_line(55, None)
    assert "[Edit in the web dashboard](" in line and line.rstrip().endswith("/#/g/55/honeypot)")
    assert "/#/c/3/g/55/honeypot" in hp._dash_line(55, 3)


def test_server_panel_screens_link_to_dashboard():
    from discord_bot.cogs import _views_server_panel as P
    mk = lambda cls, **kw: cls(55, kw.get("clone"), 1, {}, inspect=kw.get("inspect"))
    assert "/#/g/55/welcome)" in _walk_text(mk(P.WelcomeView))
    assert "/#/c/3/g/55/verification)" in _walk_text(mk(P.VerificationView, clone=3))
    assert "/#/g/55)" in _walk_text(mk(P.PremiumView))          # no matching page: overview
    home = _walk_text(mk(P.HomeView))
    assert home.count("dashboard](") == 1                          # Home keeps its single links row


def test_join_dm_has_masked_dashboard_link():
    from discord_bot.cogs._views_join_dm import build_join_dm_view
    v = build_join_dm_view(55, None)
    assert "[Open dashboard](" in _walk_text(v) and "/#/g/55)" in _walk_text(v)
