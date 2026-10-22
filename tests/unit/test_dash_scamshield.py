from modules import scam_shield as ss
from utils import dash_schema as S

CH = {"text": set(), "category": set(), "voice": set()}


def v(values):
    return S.validate_values(S.BY_ID["scamshield"], values, CH, set(), False)


def test_module_registered_and_uses_discord_storage():
    m = S.BY_ID["scamshield"]
    assert (m["get"], m["set"]) == ("get_scam_shield_guild", "set_scam_shield_guild")


def test_action_options_match_discord_actions():
    f = {f["key"]: f for f in S.BY_ID["scamshield"]["fields"]}["punish_action"]
    assert tuple(o[0] for o in f["options"]) == ss.PUNISH_ACTIONS


def test_domains_are_normalized_like_discord_flow():
    clean, err = v({"allowed_domains": ["https://www.Example.com/x", "example.com", "foo.org"]})
    assert not err and clean["allowed_domains"] == ["example.com", "foo.org"]


def test_bad_domain_rejected():
    assert v({"allowed_domains": ["not a domain"]})[1]


def test_domain_limit_matches_discord():
    assert {f["key"]: f for f in S.BY_ID["scamshield"]["fields"]}["allowed_domains"]["maxitems"] == ss.MAX_ALLOWED_DOMAINS


def test_bad_action_rejected():
    assert v({"punish_action": "nuke"})[1]


def test_valid_values_pass_and_unknown_key_rejected():
    clean, err = v({"enabled": False, "punish_action": "timeout"})
    assert not err and clean == {"enabled": False, "punish_action": "timeout"}
    assert v({"bogus": 1})[1]
