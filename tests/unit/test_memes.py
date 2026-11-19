from modules import memes as mm


def test_clean_subs_only_presets_and_never_empty():
    assert mm.clean_subs(["dankmemes", "evil_nsfw", "memes"]) == ["memes", "dankmemes"]
    assert mm.clean_subs([]) == list(mm.DEFAULT_SUBS)
    assert mm.subs_from_text(None) == list(mm.DEFAULT_SUBS)
    assert mm.subs_from_text("funny,xyz") == ["funny"]


def test_interval_is_one_of_the_choices():
    assert mm.valid_interval("12") == 12
    assert mm.valid_interval(7) == mm.DEFAULT_INTERVAL
    assert mm.valid_interval("abc") == mm.DEFAULT_INTERVAL


def test_parse_meme_keeps_safe_images_only():
    ok = {"url": "https://i.redd.it/a.jpg", "title": "t", "postLink": "https://redd.it/x", "subreddit": "memes", "ups": 5}
    assert mm.parse_meme(ok)["url"] == "https://i.redd.it/a.jpg"
    assert mm.parse_meme({**ok, "nsfw": True}) is None
    assert mm.parse_meme({**ok, "spoiler": True}) is None
    assert mm.parse_meme({**ok, "url": "http://i.redd.it/a.jpg"}) is None
    assert mm.parse_meme({**ok, "url": "https://v.redd.it/video"}) is None
    assert mm.parse_meme({}) is None


def test_ultra_price_is_2_22_and_old_price_still_accepted():
    import config
    assert config.ULTRA_PACK_FEE_USD == 2.22 and config.ULTRA_PACK_LEGACY_FEE_USD == 1.99


def test_meme_button_registered_once():
    from discord_bot.cogs import _views_join_dm as jd
    assert jd._MemeButton in jd.DYNAMIC_ITEMS
    cid = "join_dm_meme:7:-"
    assert len([i for i in jd.DYNAMIC_ITEMS if i.__discord_ui_compiled_template__.match(cid)]) == 1
