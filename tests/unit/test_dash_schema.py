import pytest
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


# --- reset / import / export ---------------------------------------------------

def test_default_values_clears_ids_and_skips_unknown_defaults():
    m = S.BY_ID["antiraid"]
    d = S.default_values(m, {"enabled": False, "sensitivity": "balanced", "log_channel_id": 123, "alert_role_id": 9})
    assert d["log_channel_id"] is None and d["alert_role_id"] is None
    assert d["sensitivity"] == "balanced" and d["enabled"] is False
    assert "lockdown_minutes" not in d                    # no default supplied -> not guessed


def test_export_document_only_declared_keys():
    m = S.BY_ID["antiraid"]
    doc = S.export_document(m, {"enabled": True, "sensitivity": "strict", "secret": "x"})
    assert doc["format"] == S.EXPORT_FORMAT and doc["module"] == "antiraid"
    assert "secret" not in doc["values"] and doc["values"]["sensitivity"] == "strict"


def test_prepare_import_applies_valid_and_skips_rest():
    m = S.BY_ID["antiraid"]
    doc = {"format": S.EXPORT_FORMAT, "version": 1, "module": "antiraid",
           "values": {"sensitivity": "strict", "log_channel_id": "999", "alert_role_id": "5",
                      "filter_age_days": 7, "nonsense": 1, "response": "explode"}}
    clean, skipped, err = S.prepare_import(m, doc, {"text": {"100"}}, {"5"}, False)
    assert err is None
    assert clean == {"sensitivity": "strict", "alert_role_id": 5}
    text = " | ".join(skipped)
    assert "isn't in this server" in text and "needs Premium" in text and "not a setting" in text and "listed options" in text


def test_prepare_import_premium_applies_premium_fields():
    m = S.BY_ID["antiraid"]
    doc = {"format": S.EXPORT_FORMAT, "version": 1, "module": "antiraid", "values": {"filter_age_days": 7}}
    clean, skipped, err = S.prepare_import(m, doc, {"text": set()}, set(), True)
    assert clean == {"filter_age_days": 7} and not skipped and err is None


@pytest.mark.parametrize("doc", [None, [], {}, {"format": "other"},
                                 {"format": S.EXPORT_FORMAT, "version": 2, "module": "antiraid", "values": {"a": 1}},
                                 {"format": S.EXPORT_FORMAT, "version": 1, "module": "welcome", "values": {"enabled": True}},
                                 {"format": S.EXPORT_FORMAT, "version": 1, "module": "antiraid", "values": {}},
                                 {"format": S.EXPORT_FORMAT, "version": 1, "module": "antiraid", "values": {"zzz": 1}}])
def test_prepare_import_rejects_bad_documents(doc):
    clean, _, err = S.prepare_import(S.BY_ID["antiraid"], doc, {"text": set()}, set(), True)
    assert err and not clean
