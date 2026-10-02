"""Join DM page 1 gets 'Open server panel' appended; no existing button moves."""
import discord

from discord_bot.cogs import _views_join_dm as jd


def _bottom_labels(page):
    keys = list(jd.FEATURE_TOGGLES.keys())
    view = jd.build_join_dm_view(1, clone_id=None, feature_keys=keys, page=page, intro="x")
    rows = [c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)]
    last = rows[-1]
    return [getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None) for c in last.children]


def test_first_page_panel_button_is_appended_last():
    labels = _bottom_labels(0)
    assert labels[-1] == "Open server panel"
    assert len(labels) <= 5
    assert labels[:-1] == [l for l in labels if l != "Open server panel"]


def test_other_pages_unchanged():
    assert "Open server panel" not in _bottom_labels(1)
