import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401


def _welcome():
    return S.BY_ID["welcome"]


def test_default_values_only_declared_keys():
    cfg = {"enabled": False, "channel_id": None, "message_template": None, "card_theme": "wolf",
           "avatar_shape": "circle", "accent_color": "#5865F2", "background_color": "#2b2d31", "use_template": True,
           "secret": "x"}
    vals, left = S.default_values(_welcome(), cfg)
    assert "secret" not in vals and vals["enabled"] is False and vals["card_theme"] == "wolf"
    assert vals["channel_id"] is None and left == []


def test_default_values_leave_inexpressible_fields():
    vals, left = S.default_values(S.BY_ID["automod"], {})        # numbers with no default can't be expressed
    assert "Timeout length (minutes)" in left and "timeout_minutes" not in vals


def test_export_import_roundtrip():
    m = _welcome()
    payload = S.export_payload(m, {"enabled": True, "channel_id": 100, "message_template": "hi", "card_theme": "wolf",
                                   "avatar_shape": "circle", "accent_color": "#5865F2", "background_color": "#2b2d31",
                                   "use_template": True}, "2026-10-08T00:00:00Z")
    vals, skipped, err = S.import_values(m, payload, {"text": {"100"}}, set(), False)
    assert err is None and skipped == [] and vals["channel_id"] == 100 and vals["enabled"] is True


def test_import_rejects_wrong_file():
    m = _welcome()
    for bad in (None, [], {"format": "x"}, {"format": S.EXPORT_FORMAT, "version": 99},
                {"format": S.EXPORT_FORMAT, "version": 1, "module": "automod", "values": {"a": 1}},
                {"format": S.EXPORT_FORMAT, "version": 1, "module": "welcome", "values": {}}):
        assert S.import_values(m, bad, {"text": set()}, set(), True)[2]


def test_import_skips_foreign_ids_unknown_keys_and_premium():
    m = _welcome()
    payload = {"format": S.EXPORT_FORMAT, "version": 1, "module": "welcome",
               "values": {"channel_id": "999", "evil": 1, "card_theme": "reaper", "enabled": True}}
    vals, skipped, err = S.import_values(m, payload, {"text": {"100"}}, set(), False)
    assert err is None and vals == {"enabled": True} and len(skipped) == 3


def test_reset_route_writes_defaults_and_requires_auth(env):
    fake, _ = env
    assert call("POST", body={"action": "reset", "guild_id": str(GUILD), "module": "welcome"}, token=None)[0] == 401
    assert call("POST", body={"action": "reset", "guild_id": "999", "module": "welcome"})[0] == 403
    assert call("POST", body={"action": "reset", "guild_id": str(GUILD), "module": "nope"})[0] == 404
    st, p, _ = call("POST", body={"action": "reset", "guild_id": str(GUILD), "module": "welcome"})
    assert st == 200 and fake.saved and fake.saved[-1][0] == GUILD


def test_import_check_never_writes(env):
    fake, _ = env
    data = {"format": S.EXPORT_FORMAT, "version": 1, "module": "welcome", "values": {"enabled": True}}
    st, p, _ = call("POST", body={"action": "import_check", "guild_id": str(GUILD), "module": "welcome", "data": data})
    assert st == 200 and p["values"] == {"enabled": True} and not fake.saved
    assert call("POST", body={"action": "import_check", "guild_id": str(GUILD), "module": "welcome", "data": {"x": 1}})[0] == 422


def test_export_route(env):
    st, p, _ = call("GET", {"action": "export", "guild_id": str(GUILD), "module": "welcome"})
    assert st == 200 and p["file"]["format"] == S.EXPORT_FORMAT and p["filename"].endswith(".json")
