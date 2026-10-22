from utils import dash_schema as S

CH = {"text": {"100", "101"}, "category": {"200"}, "voice": {"300"}}
ROLES = {"500", "501"}


def v(mod, values, premium=False):
    return S.validate_values(S.BY_ID[mod], values, CH, ROLES, premium)


def test_can_manage_rules():
    roles = {1: 0, 10: S.MANAGE_GUILD, 11: 0, 12: S.ADMINISTRATOR}
    assert S.can_manage(5, 5, [], roles, 1)                      # owner
    assert S.can_manage(5, 6, [10], roles, 1)                    # Manage Server
    assert S.can_manage(5, 6, [11, 12], roles, 1)                # Administrator
    assert not S.can_manage(5, 6, [11], roles, 1)                # nothing useful
    assert S.can_manage(5, 6, [], {1: S.MANAGE_GUILD}, 1)        # @everyone can grant it


def test_manageable_guild_filter():
    out = S.guild_list_manageable([
        {"id": "1", "name": "a", "owner": True, "permissions": "0"},
        {"id": "2", "name": "b", "permissions": str(S.MANAGE_GUILD)},
        {"id": "3", "name": "c", "permissions": str(S.ADMINISTRATOR)},
        {"id": "4", "name": "d", "permissions": "2048"},
        {"id": "5", "name": "e", "permissions": "garbage"},
    ])
    assert [g["id"] for g in out] == ["1", "2", "3"]


def test_every_module_is_well_formed():
    assert len({m["id"] for m in S.MODULES}) == len(S.MODULES)
    for m in S.MODULES:
        assert m["category"] in S.CATEGORIES and m["fields"]
        assert len({f["key"] for f in m["fields"]}) == len(m["fields"])
        for f in m["fields"]:
            assert f["type"] in ("toggle", "number", "text", "textarea", "select", "color", "list", "channel", "role")
            if f["type"] == "number":
                assert f["min"] <= f["max"]
            if f["type"] == "select":
                assert f["options"]


def test_public_schema_hides_db_names():
    for m in S.public_schema()["modules"]:
        assert "get" not in m and "set" not in m


def test_toggle_number_select():
    ok, err = v("automod", {"word_filter_enabled": True, "anti_mention_threshold": "7", "action": "kick"})
    assert not err and ok == {"word_filter_enabled": True, "anti_mention_threshold": 7, "action": "kick"}
    for bad in ({"word_filter_enabled": "yes"}, {"anti_mention_threshold": 1}, {"anti_mention_threshold": "x"},
                {"action": "nuke"}, {"nonsense": 1}, {}, "str"):
        assert v("automod", bad)[1], bad


def test_numeric_select_returns_int():
    assert v("antiraid", {"lockdown_minutes": "30"})[0] == {"lockdown_minutes": 30}


def test_channels_and_roles_must_belong_to_guild():
    assert v("welcome", {"channel_id": "100"})[0] == {"channel_id": 100}
    assert v("welcome", {"channel_id": None})[0] == {"channel_id": None}
    assert v("welcome", {"channel_id": "999"})[1]
    assert v("welcome", {"channel_id": "abc"})[1]
    assert v("tickets", {"category_id": "100"})[1]            # a text channel is not a category
    assert v("tickets", {"category_id": "200", "support_role_id": "500"})[0] == {"category_id": 200, "support_role_id": 500}
    assert v("tickets", {"support_role_id": "1"})[1]


def test_colour_text_and_list():
    assert v("welcome", {"accent_color": "#5865F2"})[0] == {"accent_color": "#5865f2"}
    assert v("welcome", {"accent_color": "blue"})[1]
    assert v("welcome", {"message_template": "x" * 501})[1]
    assert v("welcome", {"message_template": "  hi {member}  "})[0] == {"message_template": "hi {member}"}
    assert v("automod", {"banned_words": [" Foo ", "foo", "", "bar"]})[0] == {"banned_words": ["Foo", "bar"]}
    assert v("automod", {"banned_words": ["x" * 61]})[1]
    assert v("automod", {"banned_words": "foo"})[1]


def test_premium_gating():
    assert v("antiraid", {"filter_age_days": 7}, premium=False)[1]
    assert v("antiraid", {"joiner_action": "quarantine"}, premium=False)[1]
    assert not v("antiraid", {"joiner_action": "kick"}, premium=False)[1]
    assert v("antiraid", {"filter_age_days": 7, "joiner_action": "quarantine"}, premium=True)[0]
    assert v("honeypot", {"action": "kick"}, premium=False)[1]
    assert not v("honeypot", {"action": "kick"}, premium=True)[1]


def test_economy_min_max():
    assert v("economy", {"work_min": 50, "work_max": 10})[1]
    assert not v("economy", {"work_min": 10, "work_max": 50})[1]


def test_export_stringifies_snowflakes():
    out = S.export_values(S.BY_ID["welcome"], {"enabled": 1, "channel_id": 123456789012345678, "message_template": "hi"})
    assert out["channel_id"] == "123456789012345678" and out["enabled"] is True and out["accent_color"] is None
    assert S.export_values(S.BY_ID["automod"], {"banned_words": None})["banned_words"] == []


# ───────── card design (Customize Card + Studio options) ─────────

def test_card_look_options_match_the_renderer():
    """Drift guard: every choice the dashboard offers is one the card renderer accepts, and the keys line up."""
    from modules import welcome_card as W
    from modules.welcome_studio import STUDIO_KEYS
    m = S.BY_ID["cardlook"]
    assert {f["key"] for f in m["fields"]} == set(S.CARD_LOOK_KEYS)
    assert set(S.CARD_LOOK_KEYS) <= set(W.ULTRA_DEFAULTS)
    assert {f["key"] for f in m["fields"] if f.get("premium")} == (set(S.CARD_LOOK_KEYS) & set(STUDIO_KEYS))
    allowed = {
        "layout": set(W.ULTRA_LAYOUTS), "banner": set(W.ULTRA_BANNERS), "dim": set(W.ULTRA_DIM_ALPHA),
        "avatar_side": set(W.ULTRA_AVATAR_SIDES), "text_color": set(W.ULTRA_TEXT_COLORS) | {W.ULTRA_AVATAR_COLOR},
        "font": set(W.ULTRA_FONTS), "bracket": set(W.ULTRA_BRACKETS), "ratio": set(W.ULTRA_RATIOS) | {W.ULTRA_RATIO_AUTO},
        "focus": set(W.ULTRA_FOCUS), "zoom": set(W.ULTRA_ZOOMS), "text_size": set(W.ULTRA_TEXT_SIZES), "frame": set(W.ULTRA_FRAMES),
        "frame_color": set(W.ULTRA_TEXT_COLORS), "heading_font": set(W.ULTRA_FONTS) | {"same"},
        "text_outline": set(W.ULTRA_OUTLINES) | {"auto"}, "brightness": set(W.ULTRA_BRIGHTNESS), "effect": set(W.ULTRA_EFFECTS),
        "avatar_pos": set(W.ULTRA_AVATAR_POS), "text_pos": set(W.ULTRA_TEXT_POS) | {"auto"},
    }
    for f in m["fields"]:
        if f["key"] in allowed:
            assert {value for value, _ in f["options"]} == allowed[f["key"]], f["key"]
    for k in ("pan_x", "pan_y"):
        f = next(x for x in m["fields"] if x["key"] == k)
        assert (f["min"], f["max"]) == (-W.ULTRA_PAN_LIMIT, W.ULTRA_PAN_LIMIT)
    for k in ("text_dx", "text_dy"):
        f = next(x for x in m["fields"] if x["key"] == k)
        assert (f["min"], f["max"]) == (-W.ULTRA_NUDGE_LIMIT, W.ULTRA_NUDGE_LIMIT)
    # a round trip through the renderer's own parser keeps every value the dashboard can send
    sample = {"layout": "centered", "ratio": "auto", "zoom": 150, "frame": "polaroid", "effect": "sparkle", "text_pos": "top-right"}
    assert all(W.parse_ultra_options(sample)[k] == val for k, val in sample.items())


def test_card_look_validation_and_studio_gate():
    clean, errs = v("cardlook", {"layout": "centered", "font": "script", "heading": "Hello!"})
    assert not errs and clean == {"layout": "centered", "font": "script", "heading": "Hello!"}
    assert v("cardlook", {"font": "comic sans"})[1]
    assert v("cardlook", {"pan_x": 500}, premium=True)[1]
    clean, errs = v("cardlook", {"frame": "film"})
    assert errs and "Premium" in errs[0]
    clean, errs = v("cardlook", {"frame": "film", "zoom": 200, "pan_x": -20, "text_pos": "bottom"}, premium=True)
    assert not errs and clean == {"frame": "film", "zoom": 200, "pan_x": -20, "text_pos": "bottom"}


# ───────── welcome: DM delivery options ─────────

def test_welcome_dm_options_validate():
    clean, errs = v("welcome", {"delivery_mode": "dm", "dm_fallback_to_channel": True, "dm_message_template": "Hi {member}"})
    assert not errs and clean == {"delivery_mode": "dm", "dm_fallback_to_channel": True, "dm_message_template": "Hi {member}"}
    assert v("welcome", {"delivery_mode": "carrier pigeon"})[1]
    assert v("welcome", {"dm_message_template": "x" * 301})[1]
    clean, errs = v("welcome", {"dm_message_template": "   "})
    assert not errs and clean["dm_message_template"] is None


def test_welcome_dm_fields_use_their_own_setters():
    m = S.BY_ID["welcome"]
    assert set(m["own_setters"]) == {"dm_fallback_to_channel", "dm_message_template"}
    keys = {f["key"] for f in m["fields"]}
    assert set(m["own_setters"]) <= keys


# ───────── join verification: premium extras ─────────

def test_verification_extras_need_premium():
    for key, val in (("panel_title", "Hi"), ("panel_color", "#ff0000"), ("kick_after_minutes", 30),
                     ("min_account_age_days", 3), ("log_channel_id", "100"), ("welcome_text", "Welcome {user}")):
        clean, errs = v("verification", {key: val})
        assert not clean and errs and "Premium" in errs[0], key
        clean, errs = v("verification", {key: val}, premium=True)
        assert not errs and key in clean, key


def test_verification_color_is_stored_as_int_and_shown_as_hex():
    clean, errs = v("verification", {"panel_color": "#5865F2"}, premium=True)
    assert not errs and clean["panel_color"] == 0x5865F2
    clean, errs = v("verification", {"panel_color": ""}, premium=True)
    assert not errs and clean["panel_color"] is None
    assert v("verification", {"panel_color": "red"}, premium=True)[1]
    m = S.BY_ID["verification"]
    assert S.export_values(m, {"panel_color": 0x5865F2})["panel_color"] == "#5865f2"
    assert S.export_values(m, {"panel_color": None})["panel_color"] is None


def test_verification_off_choices_are_stored_as_null():
    clean, errs = v("verification", {"kick_after_minutes": 0, "min_account_age_days": 0}, premium=True)
    assert not errs and clean == {"kick_after_minutes": None, "min_account_age_days": None}
    clean, _ = v("verification", {"kick_after_minutes": 60, "min_account_age_days": 7}, premium=True)
    assert clean == {"kick_after_minutes": 60, "min_account_age_days": 7}
    assert v("verification", {"kick_after_minutes": 5}, premium=True)[1]
    m = S.BY_ID["verification"]
    ex = S.export_values(m, {"kick_after_minutes": None, "min_account_age_days": 3})
    assert ex["kick_after_minutes"] == 0 and ex["min_account_age_days"] == 3


def test_verification_banner_must_be_https():
    assert v("verification", {"panel_image_url": "https://x.test/a.png"}, premium=True)[0]
    for bad in ("http://x.test/a.png", "javascript:alert(1)", "https://x.test/a b.png"):
        assert v("verification", {"panel_image_url": bad}, premium=True)[1], bad
    clean, errs = v("verification", {"panel_image_url": ""}, premium=True)
    assert not errs and clean["panel_image_url"] is None


def test_verification_reset_defaults_clear_extras():
    m = S.BY_ID["verification"]
    vals, left = S.default_values(m, {"mode": "captcha", "timeout_seconds": 300, "max_attempts": 3, "enabled": False,
                                      "panel_title": None, "panel_color": None, "kick_after_minutes": None})
    assert vals["panel_color"] is None and vals["kick_after_minutes"] is None and vals["panel_title"] is None


# ───────── birthdays ─────────

def test_birthday_fields_match_the_bot():
    from modules import birthday_store as BS, birthdays as BD
    m = S.BY_ID["birthdays"]
    keys = {f["key"] for f in m["fields"]}
    assert keys <= set(BS._CONFIG_FIELDS) and keys == set(BS._CONFIG_FIELDS)
    lim = {f["key"]: f for f in m["fields"]}
    assert (lim["utc_offset_hours"]["min"], lim["utc_offset_hours"]["max"]) == (BD.MIN_OFFSET, BD.MAX_OFFSET)
    assert (lim["extra_messages"]["maxitems"], lim["extra_messages"]["maxlen"]) == (BD.MAX_EXTRA_MESSAGES, BD.EXTRA_MESSAGE_LEN)
    assert (lim["embed_title"]["maxlen"], lim["embed_footer"]["maxlen"], lim["message"]["maxlen"]) == (BD.TITLE_MAX, BD.FOOTER_MAX, BD.MAX_MESSAGE_LEN)
    free = {f["key"] for f in m["fields"] if not f.get("premium")}
    assert free == {"enabled", "channel_id", "ping_role_id", "message", "utc_offset_hours", "announce_hour"}


def test_birthday_validation():
    clean, errs = v("birthdays", {"enabled": True, "channel_id": "100", "utc_offset_hours": 0, "announce_hour": 9, "message": "Happy {user}!"})
    assert not errs and clean["announce_hour"] == 9 and clean["channel_id"] == 100
    assert v("birthdays", {"announce_hour": 24})[1] and v("birthdays", {"utc_offset_hours": 15})[1]
    assert v("birthdays", {"embed_title": "Yay"})[1]                                     # Premium
    clean, errs = v("birthdays", {"embed_color": "#EE79C2", "show_avatar": False, "extra_messages": ["a", "b"]}, premium=True)
    assert not errs and clean == {"embed_color": 0xEE79C2, "show_avatar": False, "extra_messages": ["a", "b"]}
    assert v("birthdays", {"extra_messages": list("abcdef")}, premium=True)[1]          # max 5
    m = S.BY_ID["birthdays"]
    assert S.export_values(m, {"show_avatar": None})["show_avatar"] is True              # unset means shown
    assert S.export_values(m, {"show_avatar": False})["show_avatar"] is False
