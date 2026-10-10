"""Discord welcome wizard: every cover has a short description; a locked cover offers Premium AND the one-time pack."""
import asyncio

import config
from discord_bot.cogs import _views_welcome as W
from modules.welcome_card import THEME_BACKGROUNDS


def test_every_cover_has_a_name_and_a_short_blurb():
    assert set(W.LOOK_BLURBS) == set(W.LOOK_NAMES) == set(THEME_BACKGROUNDS) == {v for v, _ in W.WelcomeCardLookSelect._LOOKS}
    assert all(0 < len(t) <= 60 for t in W.LOOK_BLURBS.values())


def _options(cfg):
    async def go():
        return W.WelcomeCardLookSelect(1, None, 2, cfg).item.options
    return asyncio.run(go())


def test_picker_shows_a_description_under_every_cover_within_discords_limit():
    for cfg in ({}, {"card_pack_trial_used": True}, {"card_pack_unlocked": True}):
        opts = _options(cfg)
        assert len(opts) == 6
        assert all(o.description and len(o.description) <= 100 for o in opts), cfg
    locked = {o.value: o for o in _options({"card_pack_trial_used": True})}
    assert locked["reaper"].label.startswith("🔒") and "Premium or one-time pack" in locked["reaper"].description
    assert not locked["wolf"].label.startswith("🔒")
    assert not any(o.label.startswith("🔒") for o in _options({"card_pack_unlocked": True}))


def test_locked_message_explains_both_options_and_names_the_cover():
    t = W._locked_text("shadow")
    assert "Shadow Monarch" in t and "Premium" in t and "One-time pack" in t and W.LOOK_BLURBS["shadow"] in t


def test_buy_view_has_premium_and_one_time_buttons_priced_from_config():
    async def go():
        return W._LockedPackBuyView(5, None)
    v = asyncio.run(go())
    labels = [c.label for c in v.children]
    assert len(labels) == 2
    assert f"${config.PREMIUM_FEE_USD:g}/month" in labels[0] and "Premium" in labels[0]
    assert f"${config.WELCOME_CARD_PACK_FEE_USD:g}" in labels[1] and "One-time" in labels[1]
